# Recover a mid-task run

```python
from pathlib import Path
from sprint import Checkpoint

async with experiment.start(directory=Path(".sprint/original"), backend="modal") as run:
    # Check progress through run.environment, then save at the desired point.
    saved = await run.checkpoint("before-next-experiment")
    # The original branch continues until stop() or context exit.

result = await experiment.run(
    directory=Path(".sprint/restored"),
    backend="modal",
    resume_from=saved,
)
```

`Checkpoint(path)` reopens a saved manifest. `saved.verify()` checks all stored
file hashes. `earlier.diff(saved)` reports added, deleted and modified paths.
Copy the entire `checkpoints/` directory, including `blobs/`, to move a snapshot.

## Saved state

- Learner and worker directories declared by `Env.state_paths` and
  `Worker.state_paths`. Defaults: `/app`, `/home/agent`, `/logs/agent`,
  `/resources/control`, `/installed-agent`.
- Native Codex or Claude Code conversation records, loaded through the harness's
  resume interface. Oracle and nop runs restart against the restored files.
- Completed and pending broker request records, collected artifacts, request
  counters and remaining worker/trial time.
- For `Agent(..., harness="bounded-codex")`, the root-owned action counter. A
  restored run keeps the original total limit and only its remaining actions.
  Missing, invalid or exhausted action state blocks restore. Sprint compilation
  installs its root-owned managed Codex profile in the learner image; custom
  external-host adapters must provision the equivalent policy themselves.
- The compiled release, including private validation, for reproducible recovery.

Choose additional persistent directories with `state_paths=(...)`. Paths must be
absolute, nonoverlapping directories outside `/proc`, `/sys` and `/dev`.
Special files such as sockets and pipes cause capture to fail. Operator transfer
files are temporary implementation details, not application state.

## Recovery behavior

Restore requires the same experiment definition and an unused output directory.
Each restore creates a separate branch with the saved remaining time; provider
startup and authentication happen again. Current support is Codex, Claude Code,
bounded Codex, oracle and nop; provider implementations must support root execution and file
transfer. Docker and Modal use the same logical-state format.

Attached Modal shell workers support `sprint-resource connect NAME`. Recovery
refreshes the saved SSH config with the recreated worker’s address and pinned
host key before the agent resumes. Keep training state under `Worker.state_paths`.

Processes restart. RAM, GPU memory and external API connections are not saved.
Training must write model, optimizer, scheduler and RNG state to checkpointed
files if it needs to continue exactly. Sprint does not replay interrupted shell
commands. The agent must inspect the restored files and feedback before retrying.
External side effects cannot be rolled back.

Capture waits for active worker requests to finish and temporarily pauses writers.
That time counts against the trial budget. A failed capture publishes no manifest
and attempts to resume the original branch. Integrity failures block restore.

Keep checkpoints private: harness files can contain credentials, and the compiled
release contains protected validation data. The content hashes detect corruption;
they do not authenticate a store against its operator.

Real provider preflight:

```sh
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/preflight.py
# Exercise direct SSH and refreshed host pins instead of brokered Python jobs:
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/preflight.py --ssh
```

## Validation

Real Modal tests cover restoring learner files, worker files, completed request
IDs, subsequent worker execution and remaining time; and native Codex/Astra
conversation resume in a fresh container. Both restored runs received the expected
reward of 1.0. The `--ssh` preflight also restored a Modal shell worker and
reused the learner’s saved SSH config to read and update its restored files;
its independent validation returned 1.0. This protocol check used CPU workers.
The bounded-Codex check also retained 4 spent actions, resumed with that offset,
finished at 9 of 20 actions and reported complete process cleanup.
Native Runpod learner recovery remains a separate integration check.
Claude Code and Docker share the recovery implementation but have
not yet received equivalent live restore tests.

The native Codex check is also runnable:

```sh
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/codex_preflight.py
# Also retain and enforce spent actions:
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/codex_preflight.py --bounded
```

Restore validates remaining trial/worker time against the original definition,
request lineage, spending reservations and permitted state roots before provisioning.
Malformed or increased budgets fail immediately. Checkpoint integrity hashes detect
corruption; the private operator-owned store is not a signature against a malicious
operator rewriting both manifests and hashes.

Each restored branch is a separate trial. Continuing multiple branches can multiply
aggregate cloud spending; per-trial reservations are not a campaign-wide invoice cap.

Resource-bearing checkpoints pin the learner queue reader and worker execution/export
helpers. Restore requires their captured hashes to match the calling Sprint build,
before writing the restored release or provisioning containers. An incompatible
checkpoint must be resumed with its original Sprint build. Helpers are never silently
upgraded inside a saved release. This conservative check also rejects helper edits
that do not change the protocol.
