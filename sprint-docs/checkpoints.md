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
Archive export streams verified chunks into the archive without a second temporary
copy of each model file. Capture and transfer still stage a complete archive;
chunk deduplication reduces stored snapshots, not network transfer.

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
Regular-file hard links, including shared uv cache files, are retained without
following symlinks outside the captured roots.

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

## External workers

An adapter using `ModalWorker` can use the same checkpoint store:

```python
saved = worker.checkpoint(Path(".sprint/worker-checkpoints"), "mid-task")
worker.close()
config = replacement.restore(saved)
```

First stop learner requests and pause the CPU agent. Unlike `LiveRun.checkpoint`,
this worker-only checkpoint leaves the original paused; close it afterward.
`replacement` must be a fresh `ModalWorker` with the same `Worker` definition and
an empty private state directory. Restore verifies files, roots, runtime helpers
and remaining lifetime before provisioning, then refreshes SSH at its configured
connection directory. Reusing the original public connection path is supported.

This saves worker files and lifetime, not CPU conversation or task-service state.
Direct Modal capture and restore stream between the remote archive and the
content-addressed store without a complete host archive. The remote worker still
needs archive space; other providers and Modal compose retain full host staging.
Restore validates file hashes and finishes upload before replacing mutable roots.
All file bytes are still transferred, even when stored chunks already exist.
`CheckpointStore.import_archive` and `export_archive` accept binary streams
without seeking. Consumer failure and cancellation drain the bounded-time Modal
reader before returning; cancellation can therefore wait for transport cleanup.
The live CPU-worker preflight rejects host-archive fallbacks, verifies a 17 MiB
fixture, and checks deletion/hard-link preservation, SSH refresh, continued file
updates, remaining lifetime and cleanup:

```sh
uv run --extra modal python examples/sprint/checkpoint/external_worker_preflight.py \
  --records /tmp/unique-worker-restore
```

## Task-service recovery

An operator can hold a running task queue at a capture boundary:

```python
with queue.pause(timeout=600):
    # CPU agent must already be paused. Active calls have durable terminal records.
    capture_state()  # Operator callback: worker, service files and conversation.
    # Restore usable worker connections before allowing this queue to continue.
```

Call this from an operator thread, not from a request handler. New requests stay
unadmitted until the context exits; active calls finish first. Timeout or service
shutdown aborts capture rather than pretending an in-flight operation is saved.
Pause time counts against the service deadline. The research adapter does not yet
coordinate these pieces into a whole-trial checkpoint.

External task services using `LocalRequestQueue(..., resume=True)` can rebuild
their indexes and republish feedback from private request/result records.
`completed(result)` runs again to rebuild those indexes; it must not dispatch
provider work. Completed requests are never executed again. An interrupted call
without a result returns `failure_stage="recovery"` and `outcome_known=false`;
Sprint cannot determine whether its external side effect occurred.
The operator must stop the original service and recover its provider resources
before restarting. This queue option does not capture service or GPU state and
is not automatically connected to `LiveRun` checkpoints.

## Live checks

Real provider preflight:

```sh
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/preflight.py
# Exercise direct SSH and refreshed host pins instead of brokered Python jobs:
uv run --no-default-groups --extra modal python examples/sprint/checkpoint/preflight.py --ssh
# CPU-only process-kill check for external task-service request recovery:
uv run python examples/sprint/checkpoint/queue_preflight.py --records /tmp/queue-check
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
Native Runpod recovery also passed through the allocated backend: worker SSH/file
state and a separate bounded-Codex native session both restored successfully.
See [allocated hosts](allocated-host.md) for prerequisites and limits.
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
