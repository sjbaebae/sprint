# Resources

The host runs orchestration and holds provider credentials. The learner runs its own code. Attached resources supply hardware or public files. Validation runs separately with protected material.

`Worker` declares hardware, image, tooling, network access and lifetime. Sprint owns provisioning, records, artifact snapshots and cleanup. The learner owns its experiments, processes and checkpoints.

## Learner-controlled GPU

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

## Pinned assets and artifacts

On Modal, `Worker(read_only_volumes={"/base": "pinned-model-assets"})` mounts an operator-prepared asset volume read-only. Use dedicated top-level mount paths; other backends reject this option. External assets are not included in mutable workspace checkpoints: retain the same immutable release when restoring. Never mount protected evaluation data in a learner resource.

`max_artifact_bytes` defaults to 1 GiB; set it explicitly for larger model packages. The root-owned exporter enforces the limit and rejects links or files changed during copying. `ModalWorker.collect()` uses a private v2 volume and returns its path, byte count and SHA-256, without relaying checkpoint bytes through the CPU host. The importer independently checks that receipt. `ModalWorker` owns its volume's lifetime; import a durable copy before closing it. Lower-level `WorkerSession(..., artifact_volume=name)` leaves volume retention to the caller.

## Validation

Submit complete artifacts to separate validation. Model packages can use `TensorModelSession`: only the package and public input tensors enter a network-blocked model worker; protected labels and scoring stay outside it. Training resource setup does not prescribe training code or certify data cleanliness.

## Verification status

The older brokered L4 preflight in `examples/sprint/worker/preflight.py` verified job submission, checkpoint reuse, file collection, teardown and independent grading. Direct SSH and custom-model evaluation have local boundary tests; their current combined live preflight is still pending. Before a campaign, verify the configured networking, account boundaries, failure feedback, artifact handoff and cleanup on the selected provider.
