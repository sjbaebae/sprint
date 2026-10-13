"""Operator transport to a disposable, network-blocked model-only sandbox.

No protected questions file, labels, scorer or credentials enter the sandbox.
Only the selected package and public input tensors cross this boundary.
"""

import asyncio
import json
import math
import shlex
import time
import uuid
from pathlib import Path

from sprint.spec import Resources, Sandbox, Worker
from sprint.workers import WorkerSession


class TensorModelSession:
    """Run a complete model package on Modal, separately from protected scoring.

    The async context owns worker cleanup. Caller-owned input records in directory
    are private; use temporary storage when evaluation examples must not persist.
    """

    def __init__(self, package, *, directory, vocabulary_size, seconds, resources=None):
        if type(vocabulary_size) is not int or vocabulary_size < 1:
            raise ValueError("vocabulary_size must be a positive integer")
        if type(seconds) is not int or seconds < 1:
            raise ValueError("seconds must be a positive integer")
        self.package = Path(package)
        self.directory = Path(directory)
        self.vocabulary_size = vocabulary_size
        self.seconds = seconds
        self.resources = resources or Resources(gpu="L4", cpus=8, memory_mb=65536)
        self.worker = None
        self.deadline = None

    @property
    def environment(self):
        if self.worker is None or self.worker.closed or self.worker.environment is None:
            raise RuntimeError("model session is not running")
        return self.worker.environment

    async def __aenter__(self):
        self.deadline = time.monotonic() + self.seconds
        try:
            async with asyncio.timeout_at(self.deadline):
                self.worker = WorkerSession(
                    Worker(
                        sandbox=Sandbox(
                            packages=(
                                "torch==2.8.0",
                                "transformers==4.57.0",
                                "safetensors==0.6.2",
                                "accelerate==1.10.1",
                                "numpy==2.3.3",
                                "peft==0.17.1",
                            ),
                            internet="none",
                        ),
                        resources=self.resources,
                        backend="modal",
                        shell=True,
                        seconds=self.seconds,
                        lifetime_seconds=self.seconds,
                    ),
                    directory=self.directory,
                )
                await self.worker.__aenter__()
                await self.worker.start()
                await self.environment.upload_file(self.package, "/tmp/model.tar")
                for name in ("tensor_model_worker.py", "tensor_model_package.py"):
                    await self.environment.upload_file(
                        Path(__file__).with_name(name), "/tmp/" + name
                    )
                # The model worker has no protected mounts. Submitted code runs as agent.
                service_command = (
                    "python /tmp/tensor_model_worker.py "
                    "--package /tmp/model.tar --channel /app/channel "
                    f"--vocabulary-size {self.vocabulary_size} "
                    f"--device {'cuda' if self.resources.gpu else 'cpu'} "
                    "; status=$?; printf '%s\\n' \"$status\" > /app/channel/exited"
                )
                result = await self.environment.exec(
                    "mkdir -p /app/channel && nohup sh -c "
                    + shlex.quote(service_command)
                    + " > /app/channel/service.log 2>&1 < /dev/null &",
                    user="agent",
                    timeout_sec=30,
                )
                if result.return_code:
                    raise RuntimeError("model inference service failed to start")
                await self.wait_file("/app/channel/ready")
                return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def wait_file(self, path):
        deadline = self.deadline
        if deadline is None:
            raise RuntimeError("model session has not started")
        while time.monotonic() < deadline:
            result = await self.environment.exec(
                "if test -f "
                + shlex.quote(path)
                + "; then exit 0; elif test -f /app/channel/exited; "
                "then exit 3; else exit 1; fi",
                user="root",
                timeout_sec=10,
            )
            if result.return_code == 0:
                return
            if result.return_code == 3:
                # Never expose model-controlled logs from a protected evaluation.
                raise RuntimeError("model inference service exited before responding")
            await asyncio.sleep(0.1)
        raise TimeoutError("model inference deadline reached")

    async def predict(self, rows, output_token_ids):
        """Deliver inputs and validate outputs within the evaluation deadline."""
        if self.deadline is None:
            raise RuntimeError("model session has not started")
        async with asyncio.timeout_at(self.deadline):
            return await self._predict(rows, output_token_ids)

    async def _predict(self, rows, output_token_ids):
        request_id = uuid.uuid4().hex
        request = self.directory / (request_id + ".json")
        request.write_text(
            json.dumps({"input_ids": rows, "output_token_ids": output_token_ids})
        )
        remote = "/app/channel/request-" + request_id + ".json"
        await self.environment.upload_file(request, remote + ".tmp")
        moved = await self.environment.exec(
            "mv " + shlex.quote(remote + ".tmp") + " " + shlex.quote(remote),
            user="root",
            timeout_sec=10,
        )
        if moved.return_code:
            raise RuntimeError("could not deliver inference inputs")
        response = "/app/channel/response-" + request_id + ".json"
        await self.wait_file(response)
        # Bound output before parsing; never deserialize learner pickle or Python.
        result = await self.environment.exec(
            "head -c 65537 " + shlex.quote(response), user="root", timeout_sec=10
        )
        output = result.stdout or ""
        if result.return_code or len(output.encode()) > 65536:
            raise ValueError("invalid model output size")
        values = json.loads(output)
        if (
            not isinstance(values, list)
            or len(values) != len(rows)
            or any(
                not isinstance(row, list)
                or len(row) != len(output_token_ids)
                or any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in row
                )
                for row in values
            )
        ):
            raise ValueError("invalid model output tensor")
        return values

    async def __aexit__(self, *_):
        if self.worker is not None:
            await self.worker.close()
