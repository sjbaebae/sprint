"""Run Sprint on an existing SSH host; sandboxes retain independent placement."""

import asyncio
import base64
import csv
import hashlib
import importlib.metadata
import io
import zipfile
import json
import shlex
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


async def runtime_wheel(directory):
    """Ship the calling Sprint version, from a checkout or an installed wheel."""
    source = Path(__file__).resolve().parents[2]
    if (source / "pyproject.toml").is_file():
        process = await asyncio.create_subprocess_exec(
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(directory),
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await process.communicate()
        if process.returncode:
            raise RuntimeError(
                "Sprint wheel build failed: " + errors.decode(errors="replace")[-4000:]
            )
        return next(directory.glob("*.whl"))
    distribution = importlib.metadata.distribution("sprint")
    target = directory / f"sprint-{distribution.version}-py3-none-any.whl"
    records = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in distribution.files or ():
            if ".." in item.parts or item.name == "RECORD" or item.suffix == ".pyc":
                continue
            if item.parts[0] != "sprint" and ".dist-info" not in item.parts[0]:
                continue
            file = Path(str(distribution.locate_file(item)))
            if file.is_symlink() or not file.is_file():
                raise ValueError("installed Sprint contains a missing file or symlink")
            data = file.read_bytes()
            archive.writestr(str(item), data)
            checksum = (
                base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                .rstrip(b"=")
                .decode()
            )
            records.append((str(item), "sha256=" + checksum, str(len(data))))
        if not any(name == "sprint/__init__.py" for name, _, _ in records):
            raise RuntimeError("cannot stage Sprint from this installation")
        record = f"sprint-{distribution.version}.dist-info/RECORD"
        text = io.StringIO()
        csv.writer(text).writerows(records + [(record, "", "")])
        archive.writestr(record, text.getvalue())
    return target


@dataclass(frozen=True)
class Host:
    """Existing trusted operator machine, e.g. a Runpod CPU Pod.

    None/local keeps execution in the calling Python process. SSH requires uv,
    Python support, and provider/model authentication on the remote host. This
    machine is not the learner sandbox and must never be exposed to the agent.
    """

    ssh: str
    identity: Path | None = None
    port: int = 22
    directory: str = "/workspace/sprint"
    uv: str = "uv"
    env: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if (
            not self.ssh
            or self.ssh.startswith("-")
            or any(c.isspace() for c in self.ssh)
        ):
            raise ValueError("ssh must be a user@host or SSH config alias")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("invalid SSH port")
        if not Path(self.directory).is_absolute() or ".." in Path(self.directory).parts:
            raise ValueError("host directory must be an absolute path")
        if any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in self.env.items()
        ):
            raise ValueError("host environment must contain strings")

    def command(self, command):
        """Use native SSH config and strict host-key checking; no shell interpolation."""
        args = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(self.port),
        ]
        if self.identity is not None:
            args += ["-i", str(Path(self.identity).expanduser())]
        return args + [self.ssh, command]

    async def exec(self, command, *, stdin=None, stdout=None):
        """Execute an operator command over SSH, retaining bounded diagnostics."""
        process = await asyncio.create_subprocess_exec(
            *self.command(command),
            stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=stdout if stdout is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, errors = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(
                f"SSH host command failed ({process.returncode}): {(errors or b'')[-8000:].decode(errors='replace')}"
            )
        return output or b""

    async def run(
        self, experiment, *, directory, backend, trials, parallel, resume_from=None
    ):
        """Stage an immutable wheel and compiled task, execute remotely, fetch records."""
        from .backends.runtime import compile_task
        from .checkpoint_store import Checkpoint
        from .live import definition
        from .results import RunResult, TrialResult

        directory = Path(directory).resolve()
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        remote = self.directory.rstrip("/") + "/runs/" + uuid4().hex
        with tempfile.TemporaryDirectory(prefix="sprint-host-") as temporary:
            temporary = Path(temporary)
            wheel = await runtime_wheel(temporary)
            task = (
                None
                if resume_from is not None
                else compile_task(experiment, directory / "task")
            )
            payload = {
                "experiment": definition(experiment),
                "backend": backend,
                "trials": trials,
                "parallel": parallel,
                "env": self.env,
            }
            checkpoint = None
            if resume_from is not None:
                checkpoint = (
                    resume_from
                    if isinstance(resume_from, Checkpoint)
                    else Checkpoint(resume_from)
                )
                checkpoint.verify()
                payload["checkpoint"] = checkpoint.path.name
            request = temporary / "request.json"
            request.write_text(json.dumps(payload))
            request.chmod(0o600)
            archive = temporary / "input.tar"
            with tarfile.open(archive, "w") as stream:
                stream.add(wheel, arcname=wheel.name)
                stream.add(request, arcname="request.json")
                if task is not None:
                    stream.add(task, arcname="task")
                if checkpoint is not None:
                    stream.add(checkpoint.store.directory, arcname="checkpoints")
            await self.exec("umask 077; mkdir -p " + shlex.quote(remote))
            with archive.open("rb") as stream:
                await self.exec("tar -xf - -C " + shlex.quote(remote), stdin=stream)
            placement = {
                "host": {"ssh": self.ssh, "port": self.port, "directory": remote},
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "learner_backend": backend,
                "workers": {
                    name: getattr(value, "backend", None) or backend
                    for name, value in experiment.env.attachments.items()
                    if hasattr(value, "seconds")
                },
            }
            (directory / "host.json").write_text(json.dumps(placement, indent=2))
            extras = {backend} | {
                w.backend or backend
                for w in experiment.env.attachments.values()
                if hasattr(w, "backend")
            }
            extras -= {"docker", "podman", "apple-container", "singularity"}
            requirement = (
                remote
                + "/"
                + wheel.name
                + ("[" + ",".join(sorted(extras)) + "]" if extras else "")
            )
            command = shlex.join(
                [
                    self.uv,
                    "run",
                    "--no-project",
                    "--python",
                    "3.12",
                    "--with",
                    requirement,
                    "python",
                    "-m",
                    "sprint.host_runner",
                    remote + "/request.json",
                ]
            )
            error = None
            try:
                with (directory / "host.stdout.log").open("wb") as output:
                    await self.exec("exec " + command, stdout=output)
            except BaseException as failure:
                error = failure
                # A second authenticated channel asks the remote runtime to clean up.
                stop = shlex.join(
                    [
                        self.uv,
                        "run",
                        "--no-project",
                        "--python",
                        "3.12",
                        "--with",
                        requirement,
                        "python",
                        "-m",
                        "sprint.host_runner",
                        remote + "/request.json",
                        "--stop",
                    ]
                )
                try:
                    await asyncio.wait_for(self.exec(stop), timeout=60)
                except Exception as cleanup:
                    (directory / "host.cleanup-error.txt").write_text(str(cleanup))
            try:
                output_archive = temporary / "records.tar"
                with output_archive.open("wb") as stream:
                    await self.exec(
                        "tar -cf - -C " + shlex.quote(remote + "/output") + " .",
                        stdout=stream,
                    )
                with tarfile.open(output_archive) as stream:
                    stream.extractall(directory, filter="data")
            except Exception:
                if error is None:
                    raise
            if error is not None:
                raise error
        payload = json.loads((directory / "result.json").read_text())
        result = RunResult(
            directory, tuple(TrialResult(**trial) for trial in payload["trials"])
        )
        return result.save()
