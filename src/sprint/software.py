"""Execute submitted software separately from hidden answers and scoring code."""

import asyncio
import os
import stat
import time
from pathlib import Path
from uuid import uuid4

from sprint.spec import Resources, Sandbox, Worker
from sprint.workers import WorkerSession


class SoftwareSession:
    """Disposable offline execution; outputs are untrusted, never a grade.

    Only submission files and explicitly supplied inputs enter the sandbox.
    The caller retains expected answers and computes scores outside it. Files
    persist between calls in one session; use separate sessions when tests need
    independent state. The records directory can contain protected inputs.
    """

    def __init__(
        self,
        submission,
        *,
        directory,
        seconds=60,
        backend="modal",
        sandbox=None,
        resources=None,
    ):
        if type(seconds) is not int or seconds < 1:
            raise ValueError("seconds must be a positive integer")
        sandbox = sandbox or Sandbox()
        if sandbox.internet != "none":
            raise ValueError("software evaluation requires an offline sandbox")
        self.submission = Path(submission)
        self.directory = Path(directory)
        self.seconds = seconds
        self.spec = Worker(
            sandbox=sandbox,
            resources=resources or Resources(),
            backend=backend,
            seconds=seconds,
            lifetime_seconds=seconds,
        )
        self.worker = None
        self.deadline = None

    def _files(self):
        """Inspect bounded regular files without executing submitted code."""
        if self.submission.is_symlink() or not self.submission.is_dir():
            raise ValueError("submission must be a regular directory")
        files, size = [], 0
        for root, directories, names in os.walk(self.submission, followlinks=False):
            for name in directories + names:
                path = Path(root) / name
                mode = path.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError("submission contains a link or special file")
                if stat.S_ISREG(mode):
                    size += path.stat().st_size
                    files.append(path)
                    if len(files) > 1000 or size > 1024**3:
                        raise ValueError("submission exceeds 1000 files or 1 GiB")
        if not files:
            raise ValueError("submission contains no files")
        return files

    async def __aenter__(self):
        files = self._files()
        self.deadline = time.monotonic() + self.seconds
        try:
            async with asyncio.timeout_at(self.deadline):
                self.worker = WorkerSession(self.spec, directory=self.directory)
                await self.worker.__aenter__()
                await self.worker.start()
                for path in files:
                    relative = path.relative_to(self.submission).as_posix()
                    await self.worker.upload_file(path, "submission/" + relative)
                return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def run(self, argv, *, stdin=b""):
        """Run an explicit argv with bounded stdin; return exit code and output.

        No shell interpolation occurs. Existing worker limits bound stdout/stderr
        to 1 MiB each and terminate descendants after execution. A nonzero exit is
        returned as execution feedback; it does not define the task's score.
        """
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(value, str) or "\0" in value for value in argv)
            or not argv[0]
            or sum(len(value.encode()) for value in argv) > 65536
        ):
            raise ValueError("argv must be a nonempty bounded string sequence")
        if not isinstance(stdin, bytes) or len(stdin) > 1024**2:
            raise ValueError("stdin must be bytes of at most 1 MiB")
        if self.worker is None or self.worker.closed or self.deadline is None:
            raise RuntimeError("software session is not running")
        async with asyncio.timeout_at(self.deadline):
            input_path = "input-" + uuid4().hex
            await self.worker.upload(stdin, input_path)
            source = (
                "import os\n"
                f"descriptor = os.open({'/app/' + input_path!r}, os.O_RDONLY)\n"
                "os.dup2(descriptor, 0)\n"
                "os.close(descriptor)\n"
                "os.chdir('/app/submission')\n"
                f"os.execvpe({argv[0]!r}, {list(argv)!r}, os.environ)\n"
            )
            return await self.worker.run(source)

    async def __aexit__(self, *_):
        if self.worker is not None:
            await self.worker.close()
