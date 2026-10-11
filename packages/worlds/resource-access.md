# Named resource access

Read-only named file attachments are implemented. Separate hardware requests already
work. Named worker submission below is the next implementation target.

```python
world = World(
    resources=Resources(cpus=2),
    attachments={
        "train": Worker(resources=Resources(gpu="L4"), sandbox=gpu_image),
        "data": Files(path=Path("datasets")),
    },
)
```

The host orchestrates. The agent has a CPU workspace and explicitly granted
operations on named attachments. A GPU worker is separate from the agent and the
private verifier. It persists within a trial so checkpoints can be reused; it is
created lazily and terminated at trial end.

## Minimal operations

- `worlds-resource list`: names and permitted operations.
- `worlds-resource run train script.py`: submit code to the named worker.
- `worlds-resource status JOB`: status and full execution feedback.
- `worlds-resource collect JOB`: retrieve allowed output files/checkpoints.
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
workers. Provider TTLs bound orphan lifetime if the host fails.

## Required preflight

Run a CPU-host trial with an attached L4 worker, execute a small GPU operation,
collect its artifact, and grade it in an independent CPU verifier. Check denied
resource names, invalid requests, failed jobs, timeout cleanup, checkpoint reuse
and blocked outbound access before considering the attachment API ready.
