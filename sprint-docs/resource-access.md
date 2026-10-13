# Named resource access

Read-only named file attachments are implemented. Separate hardware requests already
work. `WorkerSession` now provisions persistent workers and executes bounded Python jobs.
The agent-facing queue supports run/exec/status. Regular-file checkpoint collection is implemented.

```python
env = Env(
    resources=Resources(cpus=2),
    attachments={
        "train": Worker(resources=Resources(gpu="L4"), sandbox=gpu_image, shell=True),
        "data": Files(path=Path("datasets")),
    },
)
```

The host orchestrates. The agent has a CPU workspace and explicitly granted
operations on named attachments. A GPU worker is separate from the agent and the
private verifier. It persists within a trial so checkpoints can be reused; it is
created lazily and terminated at trial end.

## Minimal operations

- `sprint-resource list`: names and permitted operations.
- `sprint-resource upload train LOCAL_FILE code/train.py`: copy a file of at most 1 MiB into the worker workspace. Use read-only `Files` attachments for large initial datasets. Uploads are recorded with size and SHA-256; transfer does not establish what data training consumed.
- Operator code can use `await session.upload_file(path, "data/train.jsonl", max_bytes=1024**3)` for larger files. Sprint freezes a bounded regular-file snapshot and records its hash before transfer. This host API does not expose host paths through learner RPC.
- `sprint-resource run train script.py`: submit code to the named worker.
- `sprint-resource exec train 'nvidia-smi'`: execute a shell command when the worker grants `shell=True`.
- `sprint-resource status JOB`: status and full execution feedback.
- `sprint-resource collect REQUEST_ID FILE --wait 60`: snapshot a regular file
  beneath the worker’s `/app`, returning a read-only path in the agent sandbox.
- Read-only datasets appear under `/resources/data`.

## Transport and boundaries

Use an operator-mediated request queue and provider control-plane transfers.
The learner needs no provider credentials or outbound network route to submit a
job. The host validates bounded requests, routes execution to the granted worker,
and records trusted start/end times, configuration and outputs. Request IDs refer
to jobs within the current trial; they cannot select host paths or another trial.

The worker executes learner code without provider credentials or private grading
files. Files returned through collection cannot be symlinks or special files.
Each worker has explicit hardware, network policy and deadlines. A resource grant
does not grant access to the host, other trials or the verifier.

Queue messages and learner logs are untrusted. The operator journal records what
was actually executed. Cancellation and timeout must tear down all allocated
workers. Modal TTLs bound orphan lifetime if the host fails. Docker cleanup requires the host
to remain alive. The host deadline includes provisioning and job execution.

## Required preflight

Run a CPU-host trial with an attached L4 worker, execute a small GPU operation,
collect its artifact, and grade it in an independent CPU verifier. Check denied
resource names, invalid requests, failed jobs, timeout cleanup, checkpoint reuse
and blocked outbound access before considering the attachment API ready.

## Operator-side worker test

```python
from sprint import Resources, Worker
from sprint.workers import WorkerSession

async with WorkerSession(
    Worker(resources=Resources(gpu="L4")),
    directory=Path("run/worker"),
    backend="modal",
) as worker:
    result = await worker.run("import subprocess; print(subprocess.check_output([\"nvidia-smi\", \"-L\"]).decode())")
```

Jobs run as `agent`, reuse `/app` between successful calls and return exit code,
stdout and stderr. The operator retains submitted source hashes and UTC execution
events outside the sandbox. Context exit destroys the worker; execution exceptions
also destroy it. `Experiment.run` starts the host broker at agent start and destroys all worker
sandboxes before verification. The agent uses `sprint-resource run NAME SCRIPT.py
--wait 60` and `sprint-resource status REQUEST_ID --wait 60`. Results include actual
exit code, stdout/stderr, or an exception type and message. Python scripts are accepted by `run`; shell commands require `shell=True` and use
`exec`. Resource names must be explicitly granted. Resource requests are limited
to `Budget.resource_requests` (default 200). `Budget.worker_concurrency` bounds
concurrent requests across workers; each worker executes one job at a time.

## Verified preflights

`examples/sprint/worker/preflight.py` exercises agent submission, persistent checkpoint
reuse, file collection, cleanup and separate grading. The Modal L4 preflight
passed with outbound TCP blocked and reward 1.0. Local Docker passed with explicit
full networking; Docker Desktop rejects the requested no-network policy on this
host. These smoke tests use Harbor’s oracle harness, not a frontier model.

Worker files persist between jobs. Python `run` jobs stop worker-user processes
after each job; successful shell `exec` calls preserve them. A trusted Linux launcher
runs each Python job as `agent` with privilege escalation disabled and stops all processes
of that dedicated worker user at job completion, including detached descendants.
This applies only to attached workers, not the separate learner/harness container.
Workers require Linux pidfd support and fail closed if process cleanup is unavailable.

Timed-out jobs return exit code 124, `timed_out: true`, and captured partial output;
the worker is then closed. Output feedback is bounded to 1 MiB per stream with
explicit `stdout_truncated`/`stderr_truncated` flags. File checkpoints can still be
used by subsequent successful jobs before the worker closes.

Feedback is staged under a root-owned temporary filename and atomically published
read-only, so status readers cannot observe partial uploads. Delivery retries up to
three times without rerunning the job; each failure is timestamped in the trusted
`delivery.jsonl` journal. Exhausted retries remain an infrastructure failure, with
the original result retained on the operator host. A failed broker cannot publish
a restorable live checkpoint.

The worker launcher adopts and reaps orphan descendants after stopping them, so
repeated jobs do not accumulate their detached children as zombies under init.

A terminal broker failure interrupts only its agent trial immediately. The original
error and available records are retained; independent trials continue. The runtime
then attempts resource cleanup. This trial is an infrastructure error, not a scored
agent submission. Ordinary failed training jobs return feedback for further research.
Run `uv run python examples/sprint/failure/service_preflight.py` to exercise this
failure isolation with two Modal trials.

Queue reads retain valid requests until root-owned final feedback is published.
A lost provider response can therefore be retried without losing the submission.
The broker retries queue observation up to three times, with 35-second host bounds
per attempt, and records failures in `queue.jsonl`. Exhaustion terminates the
service and affected trial. This does not provide automatic recovery of running
jobs after host loss; debug resume uses explicitly captured checkpoint state.

Shell commands run as the worker’s learner user in `/app`. Successful commands preserve background processes; redirect their output to workspace files and inspect or stop them with later commands. Python `run` jobs retain their existing per-job process cleanup. Shell commands use GNU `timeout`: an ordinary command deadline returns exit code 124 (or 137 if forced termination is needed) and keeps the workspace. A provider timeout, trial lifetime expiry or cancellation still destroys the worker. Python-job timeouts retain their existing teardown behavior. Worker images granting shell access must provide GNU `timeout`. Shell access is disabled by default and does not change the worker’s network policy or expose provider credentials. On 2026-10-12, the Modal L4 preflight with `--shell` passed with reward 1.0: a CPU-side file upload was read by GPU code, two shell calls shared files and a background process, Python jobs reused a checkpoint, collection delivered it to a separate verifier, and a timed-out worker was closed. This validates the native resource path with the oracle harness; it does not validate a frontier-model research campaign.


## Large pinned assets

On Modal, attach an existing named volume directly to a worker:

```python
Worker(
    backend="modal",
    resources=Resources(gpu="L4"),
    shell=True,
    read_only_volumes={"/base": "pinned-model-assets"},
    max_artifact_bytes=4 * 1024**3,  # allow a full model checkpoint
)
```

Checkpoint collection defaults to 1 GiB per file. Set `max_artifact_bytes` on
the worker for larger models; the root-owned exporter and host both enforce it.
The learner cannot raise this limit through a request.

The operator prepares the volume before the trial. Sprint mounts it read-only;
it neither creates nor populates it. Mounts use dedicated top-level directories
and cannot replace the worker runtime or workspace. Other backends reject this
option. Volume contents are external assets, not part of mutable workspace
checkpoints: keep the named release immutable for reproducible restore. Never
attach protected evaluation data to a learner worker.

The 2026-10-12 Modal L4 preflight with `--shell --volume NAME` passed
with reward 1.0, including asset reads and a rejected write to the mounted volume.

## Large checkpoint handoff

For Modal-to-Modal validation, create a dedicated v2 volume and pass its name as
`WorkerSession(..., artifact_volume=name)`. Then call
`await session.collect_to_volume("output/model.safetensors")`. The receipt names
the volume, snapshot file, byte count and SHA-256. The snapshot is committed before
the receipt returns; no checkpoint bytes pass through the CPU host.

Sprint protects this mount from the learner user and uses its regular-file
exporter to reject symlinks and files changed during copying. The validator must
independently verify the receipt's size/hash and load only trusted model code.
The operator owns volume retention and deletion. Ordinary `collect()` remains
available for local artifacts and other providers.
