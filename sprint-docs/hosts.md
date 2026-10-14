# Choose the run host and sandbox backends

The **host** runs Sprint's scheduler, resource broker and provider clients. The
**learner sandbox** runs the agent. They are separate machines/containers.

Without `host=`, the calling Python process is the host. `backend="modal"` then
places the learner, verifier and default workers on Modal; it does not move the
scheduler to Modal.

## Runpod CPU host, Modal containers

```python
from pathlib import Path
from sprint import Host, Resources, Worker

host = Host(
    ssh="root@RUNPOD_PUBLIC_IP",
    port=RUNPOD_SSH_PORT,
    identity=Path("~/.ssh/runpod/runpod_base"),
    uv="/root/.local/bin/uv",
    directory="/workspace/sprint",
    env={"CODEX_FORCE_AUTH_JSON": "1"},  # existing Codex sign-in on the host
)

# In the environment's attachments:
train = Worker(backend="modal", resources=Resources(gpu="L4"))

result = await experiment.run(
    directory=Path(".sprint/runpod-modal"),
    host=host,
    backend="modal",
)
```

```text
Your machine: author definitions; stage release; retrieve review records
    └─ Runpod CPU host: Sprint scheduler + broker + provider credentials
         ├─ Modal CPU container: learner + agent harness
         ├─ Modal L4 container: submitted training jobs
         └─ Modal CPU container: private final validation
```

The Runpod Pod needs `uv`, outbound provider access, Modal authentication and the
selected model's authentication. Configure those on the trusted host before the
run. `Host.env` optionally supplies explicit environment variables over the
private SSH channel; secrets are not written to review metadata. Nothing forwards
all local environment variables or credentials automatically.

Use Runpod's **direct SSH endpoint**, or an SSH config alias supporting commands
and binary stdin/stdout. Runpod's interactive-only SSH proxy cannot carry this
transport. SSH host keys must already be trusted. No Pod creation or termination
is implicit in `Host`: its lifecycle remains operator-controlled.

Sprint stages its current wheel and the compiled task into a unique private run
directory, executes through uv, and retrieves the complete run records. Output
includes `host.json`, `placement.json`, `result.json`, `review.html`, transcripts,
worker feedback and submission snapshots. The remote directory is retained for
debugging. Credentials and protected validation must stay private.

Cancelling the awaiting task requests remote cancellation over a second SSH
connection; the runtime cleans up its sandboxes. If that connection fails, inspect
`host.cleanup-error.txt` and the retained remote directory. A lost host cannot
provide a guarantee of immediate cleanup.

## Independent validation placement

Set `Validation(bundle=Path("validation"), backend="modal")` to choose the
verifier backend separately from `experiment.run(backend=...)`. `None` keeps
the existing behavior of inheriting the run backend. Explicit validation placement
starts from its own runtime options, without learner-only kwargs or mounts;
validation’s image, hardware and network policy still come from `Validation`.

This separates placement; it does not implement a native Runpod learner provider.
The host and learner still have the roles described above. Explicit Modal validation
passed the live SSH checkpoint/restore preflight (reward 1.0); Docker-to-Modal
selection and separate cleanup settings are covered by lifecycle tests, not a
live mixed-provider trial.

## Different worker backends

`Worker.backend` overrides the run's backend; `None` inherits it. For example, a
Docker-capable host can run a CPU worker with `backend="docker"` and a GPU worker
with `backend="modal"`. Each backend must actually support the requested hardware
and network policy. Unsupported policies fail closed.

A Runpod **host** is supported through SSH. Choosing `Host` alone does not place
the learner on that host. For a dedicated prepared Pod, `backend="allocated"`
runs the learner directly under its separate account; see [allocated hosts](allocated-host.md).
That backend requires explicit operator opt-in, full internet, private run records
and separate container validation. It does not supply container isolation.

`Experiment.start()` currently controls a run in the calling process. To use its
live checkpoint API on Runpod, run that Python driver on Runpod directly. Remote
`Host` staging supports `Experiment.run()` and `resume_from=`.

## Live preflight

```sh
uv run --no-default-groups --extra modal python examples/sprint/worker/preflight.py \
  --host root@RUNPOD_PUBLIC_IP --port RUNPOD_SSH_PORT \
  --identity ~/.ssh/runpod/runpod_base --host-uv /root/.local/bin/uv --gpu L4
```

Verified on Runpod CPU → Modal L4: CUDA memory round trip, blocked learner/worker
egress, read-only data, persistent worker state, artifact collection and private
grading (reward 1.0).

Also verified: Codex/Astra authentication on the Runpod host, a complete agent
trial using the worker broker (reward 1.0), and remote cancellation with retained
records and sandbox cleanup.
