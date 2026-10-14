# Resources

The host runs orchestration and holds provider credentials. The learner runs its own code. Attached resources supply hardware or public files. Validation runs separately with protected material.

`Worker` declares hardware, image, tooling, network access and lifetime. Sprint owns provisioning, records, artifact snapshots and cleanup. The learner owns its experiments, processes and checkpoints.

## Attached GPU

Declare a persistent shell worker in the environment:

```python
Env(attachments={
    "train": Worker(
        resources=Resources(gpu="L4"),
        sandbox=Sandbox(internet="full"),
        shell=True,
        backend="modal",
        lifetime_seconds=3600,
    ),
})
```

The learner requests its connection once, then uses ordinary SSH/SCP:

```sh
sprint-resource connect train --wait 60 > connection.json
# Use the ssh_config path returned in connection.json:
ssh -F /resources/control/connections/train/config gpu 'COMMAND'
scp -F /resources/control/connections/train/config LOCAL_FILE gpu:/app/REMOTE_FILE
```

The learner image needs an SSH client and network access to the worker endpoint.
Sprint provisions the worker lazily, keeps its identity private to the trial,
pins the host key and destroys the worker during cleanup. Normal SSH exits leave
worker files and background processes available. Checkpoint recovery restores
its declared state directories and refreshes the saved connection config before
resuming the agent. Processes and GPU memory are not restored.

## External-host integration

For an external CPU host, `ModalWorker` gives the learner ordinary SSH/SCP access:

```python
import time
from sprint import Resources, Sandbox, Worker
from sprint.modal_worker import ModalWorker

resource = ModalWorker(
    Worker(
        resources=Resources(gpu="L4"), sandbox=Sandbox(internet="full"),
        shell=True, lifetime_seconds=3600, max_artifact_bytes=4 * 1024**3,
    ),
    state=private_trial_directory,
    connection_directory=public_connection_directory,
    deadline=time.time() + 3600,
    trial=trial_id,
)
try:
    config = resource.connect()
    # Give the learner the config path and keep the resource alive:
    # ssh -F CONFIG gpu 'COMMAND'
    # scp -F CONFIG LOCAL_FILE gpu:/app/REMOTE_FILE
finally:
    resource.close()
```

Run setup as the operator. The state directory must already exist and be private. The connection directory's parent must be learner-readable and operator-owned. Only the copied learner key is learner-owned. SSH pins the host key, allows only `agent`, and disables root login and forwarding. Provider credentials and protected evaluation files are not mounted.

The learner can install packages, download data, write training code, manage processes and export its model within the configured permissions. Disconnecting SSH does not reset the resource. Its provider lifetime bounds orphan execution after host failure. `cleanup_resources(state)` retries recorded sandbox/volume cleanup without skipping other resources after an individual failure.

Direct SSH does not produce broker command records. Retain the agent transcript and configure observations when needed. `resource.observe()` samples bounded files and sockets; it is not complete network or training-consumption provenance. `resource.collect(relative_path)` freezes a regular artifact under `/app` into private provider storage. The caller decides how to import and evaluate it.

## Brokered jobs

`Env(attachments={"train": Worker(...)})` currently exposes the named worker through the resource broker:

```sh
sprint-resource list
sprint-resource upload train LOCAL_FILE REMOTE_FILE --wait 60
sprint-resource run train experiment.py --wait 60
sprint-resource exec train 'COMMAND' --wait 60
sprint-resource status REQUEST_ID --wait 60
sprint-resource collect REQUEST_ID checkpoint.pt --wait 60
```

Shell calls require `shell=True`. Python jobs stop worker-user processes after each job. Shell calls preserve files and unrelated background processes, including after an ordinary command timeout (exit code 124). Lifetime expiry, cancellation and provider failure close the worker. Feedback records actual exit status, bounded stdout/stderr and explicit truncation flags. Delivery retries do not rerun jobs.

`Budget.resource_requests` limits broker requests; `Budget.worker_concurrency` limits concurrent requests across workers. Each worker serializes its own jobs. `Budget.worker_spend` reserves declared worker lifetimes using `Worker.hourly_cost`; every worker needs a rate when that limit is enabled. This is a configured estimate, not provider billing. These broker limits do not count commands issued over direct SSH; the learner action budget, resource lifetime and provider allocation remain separate controls.

`Files(Path("datasets"))` attaches public read-only files under `/resources/NAME`. A resource grant never grants access to another trial, host credentials or the verifier.

## Trusted task services

For operator-side evaluation or submission handlers, attach
`Service(Path("service"))` alongside any workers. Its private bundle exports:

```python
async def handle(context, request):
    # Persist resumable state under context.state.
    # Use await context.worker("train") for a granted worker session.
    context.record("measurement", {"accuracy": 0.42})
    return {"accepted": True}
```

The learner calls `sprint-resource call NAME INPUT.json --wait 60`. Handlers
receive JSON and return JSON; they run as trusted operator code, never as a
place to execute learner-supplied Python. Return explicit safe rejection
messages for invalid submissions. Unexpected exceptions retain a private
traceback and return a generic error.

`context.record(event, payload)` deliberately publishes JSON evidence in
`context.records / "sprint-resources/service-NAME.jsonl"` for validation and in
the run review. Redact protected examples and credentials first. Other service
working files remain private; they are not copied wholesale to the verifier.

Calls share the broker's request budget and checkpoint boundary. Checkpoints
retain service files and request history alongside the conversation and worker
files. Module globals and external systems are not restored: write durable
state under `context.state` and obtain workers through `context.worker` so
Sprint owns their recovery and cleanup. Each service serializes its calls.

A granted worker session supports `await worker.observe("/app", start_after="")`
for the same bounded file/socket observations used by the external Modal adapter.
Keep cursors and audit state under `context.state`; publish only deliberate,
redacted evidence with `context.record`. Skipped or changing files are coverage
gaps, and socket samples do not establish complete download provenance.

## Pinned assets and artifacts

On Modal, `Worker(read_only_volumes={"/base": "pinned-model-assets"})` mounts an operator-prepared asset volume read-only. Use dedicated top-level mount paths; other backends reject this option. External assets are not included in mutable workspace checkpoints: retain the same immutable release when restoring. Never mount protected evaluation data in a learner resource.

`max_artifact_bytes` defaults to 1 GiB; set it explicitly for larger model packages. The root-owned exporter enforces the limit and rejects links or files changed during copying. `ModalWorker.collect()` uses a private v2 volume and returns its path, byte count and SHA-256, without relaying checkpoint bytes through the CPU host. The importer independently checks that receipt. `ModalWorker` owns its volume's lifetime; import a durable copy before closing it. Lower-level `WorkerSession(..., artifact_volume=name)` leaves volume retention to the caller.

## Validation

Submit complete artifacts to separate validation. Model packages can use `TensorModelSession`: only the package and public input tensors enter a network-blocked model worker; protected labels and scoring stay outside it. Training resource setup does not prescribe training code or certify data cleanliness.

## Verification status

The older brokered L4 preflight in `examples/sprint/worker/preflight.py` verified job submission, checkpoint reuse, file collection, teardown and independent grading. Direct SSH/SCP, process control, artifact import and isolated custom-model evaluation have also been checked with a Runpod CPU host and Modal L4. The exported baseline reproduced its development accuracy, and inference cleanup was verified. Before a campaign, verify the configured networking, account boundaries, failure feedback, artifact handoff and cleanup on the selected provider.

## External-host request handling

`LocalRequestQueue` supplies file request/response transport for host-side handlers.
It serializes calls by default. Set `groups={"evaluate": "evaluation", "connect": "gpu", "collect": "gpu"}`
to let independent resources progress concurrently while keeping each resource serial.
Each group has one dedicated thread; completion callbacks run on the host thread.
A busy rejection names the active request and resource group. Fast metadata-only
handlers can use `inline_kinds`; they must not block the host loop.

The queue stops admitting requests at its deadline and drains admitted work.
Call `queue.stop()` from a service termination handler for the same graceful drain,
so outer audit and teardown code can run before the host's shutdown grace expires.
Handlers must enforce their own provider deadlines and cancellation. Periodic idle
observations run only when every group is idle. Grouping changes scheduling, not
resource permissions or spending limits.

## Recovery after a host interruption

Keep operator-owned `*.call.json`, `*.sandbox.json` and `*.artifact-volume.json`
records outside learner storage. `cleanup_resources(state)` cancels creating calls
first, then stops sandboxes and deletes volumes. It retains failed work for retry.
Use `terminate_containers: true` only for exclusively owned function containers.

For remote inference, pre-record a fresh UUID as `session_id` before dispatch and
pass it to `TensorModelSession(..., session_id=identity)`. Sprint attaches that ID
to the Modal sandbox. Recovery can find it even if the host died before saving
`sandbox_id`. A session record contains `{"session_id": "UUID_HEX", "terminated": false}`.
Provider lifetime limits remain the fallback when recovery cannot reach Modal.

## Recover after worker-owner failure

Native Modal workers write `worker.sandbox.json` before provisioning and retain
provider identity and termination status outside the learner. If the owner is
confirmed dead, the operator can retry cleanup for each affected worker directory:

```python
from sprint.modal_worker import cleanup_resources
cleanup_resources(private_worker_directory)
```

Do not run this against an active trial. Cleanup failures raise and retain the
unfinished identities. A creation request with an unknown outcome remains
unresolved instead of being marked successfully cleaned. Provider lifetime is
still the fallback if no operator survives. Whole-host outage recovery is not
automatically scheduled by this helper.

The live CPU check killed a native worker owner with SIGKILL, observed the worker
still running, then terminated it from retained records and kept its execution
journal:

```sh
uv run --extra modal python examples/sprint/cancellation/preflight.py \
  --host-loss --records /tmp/unique-owner-loss
```

Standard `result.json` includes unresolved `cleanup_errors` when present, matching
the warning in `review.html`.

A service may also export `async finalize(context)` for its final audit. Services
with restored private state are loaded for this audit even if no new request
arrives after recovery. Sprint calls
it once per broker shutdown, before destroying existing workers, with a 60-second
limit. It cannot provision new workers during shutdown. Record redacted audit
results with `context.record()`. Failure is retained and reported while worker
cleanup still proceeds. Cleanup retries do not repeat the callback; audit code
must use durable state to remain safe across recovery into another broker.
