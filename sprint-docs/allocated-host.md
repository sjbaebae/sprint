# Run the learner directly on an allocated host

Use `backend="allocated"` when a dedicated Linux CPU Pod cannot create nested
containers. Sprint runs the learner as `agent`; the calling root process owns
orchestration, private records and provider credentials. Validation and attached
workers use explicit container backends such as Modal.

This is account separation on an operator-controlled machine, not a container or
network-isolation boundary. Only full internet is supported. Use one dedicated
account and one concurrent trial per host. The supported harnesses are
`bounded-codex`, trusted `oracle` references and `nop`.

## Prepare once

As the operator, provision the `agent` account, Python, the selected harness and
analysis tools. Codex must match `Agent.options.version`; a mismatch fails setup
instead of installing another CLI or Node toolchain in the learner home. `Sandbox.setup`, packages and custom images are rejected here;
they describe container builds, not host installation.

Create a private root-owned state directory, then `/etc/sprint/allocated.json`
with mode `0600` under a root-owned, non-writable `/etc/sprint`:

```json
{
  "hostname": "ACTUAL_HOSTNAME",
  "uid": 1000,
  "workspace": "/app/work",
  "state": "/root/sprint-allocated-state"
}
```

The hostname and UID must match this host's `agent` account. The workspace must
be below `/app`; the state directory and its ancestors must remain protected
from learner writes. A missing marker, identity mismatch, existing lease or busy
account prevents startup. A busy-account refusal does not kill existing processes.

Starting a trial archives the previous workspace, home, installed harness,
resource files, agent logs and temporary harness state under the private state
directory. It does not delete that history. Large trees crossing filesystems
may require copying; retain enough disk space. Stop reaps the leased learner
account and releases its lease. A host crash may leave the lease for operator
inspection; Sprint does not steal stale leases or kill processes based on age.

## Define the environment

```python
from pathlib import Path
from sprint import Agent, Budget, Env, Experiment, Resources, Sandbox, Task, Validation, Worker

experiment = Experiment(
    task=Task("YOUR OBJECTIVE", submission="/app/work/submission"),
    env=Env(
        sandbox=Sandbox(internet="full"),
        files=Path("workspace"),
        state_paths=(
            "/app/work", "/home/agent", "/logs/agent",
            "/resources/control", "/installed-agent",
        ),
        attachments={"train": Worker(
            backend="modal", resources=Resources(gpu="L4"),
            sandbox=Sandbox(internet="full"), shell=True,
        )},
    ),
    agent=Agent("gpt-6-astra", harness="bounded-codex", options={"version": "0.162.1"}),
    validation=Validation(Path("validation"), backend="modal"),
    budget=Budget(seconds=3600),
)

async with experiment.start(directory=Path(".sprint/original"), backend="allocated") as run:
    # Inspect progress, then capture at the desired point.
    saved = await run.checkpoint("mid-task")

result = await experiment.run(
    directory=Path(".sprint/restored"), backend="allocated", resume_from=saved,
)
```

The public workspace is staged at the marker's workspace path. Put the submission
inside it. State roots are limited to that workspace and the listed learner harness
directories, so restore cannot replace operator system directories. Name the
workspace directly in `state_paths`; avoid `/app`, which may
contain unrelated historical material. Checkpoints retain the same file, native
session and budget state as other Sprint backends. They restart processes.

Run records are private to the operator. Authenticated harness credentials are
intentionally available to the harness; provider credentials are not inherited
by learner commands. Protected evaluation runs on its separate backend.

## Live checks

On the prepared host, with Modal authentication:

```sh
uv run python examples/sprint/checkpoint/preflight.py --allocated --ssh
uv run python examples/sprint/checkpoint/codex_preflight.py --allocated --bounded
```

The first check restored native Runpod learner files, a Modal CPU worker's files,
SSH connection and remaining time, then received private validation reward `1.0`.
The native bounded-Codex check also passed: it restored the conversation and
4 spent actions, finished at 9 of 20 actions with reward `1.0`, and completed
process cleanup. It verified that the learner could not read operator run records.
This backend has not yet replaced the research repository's task-specific adapter.
