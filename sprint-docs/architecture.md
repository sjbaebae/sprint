# Architecture contract

Authors supply the task and validation. Reuse a world, attach resources and select
an agent. Provider configuration stays behind execution adapters.

| Layer | Owns |
|---|---|
| Env | Public assets, tools and resource access |
| Sandbox | Isolation, image, tooling and network policy |
| Resources | Hardware capacity and named resource capabilities |
| Task | Objective, constraints and submission contract |
| Validation | Private verification, rubric, evidence and violations |
| Agent | Model and harness |
| Execution | Provisioning, budgets, transfers and teardown |
| Records | Events, transcripts, snapshots, diffs and results |

## Host and workers

The host runs orchestration. A world worker and a verifier have independent
resource requests. GPU capacity is explicit; it is never inherited by the
verifier. Multiple named attachments should grant explicit operations, such as
executing a job, reading a dataset or accessing a checkpoint. Capacity and access
permissions are separate. Provider credentials stay on the operator side.

The current release implements independent world/verifier capacity, named read-only
file attachments and persistent workers with a host-mediated run/status/collect queue.
Workers are created lazily and destroyed before verification.

## Implemented lifecycle

Compile an inspectable task, provision the learner, run its harness, collect the
artifact and agent records, stop the learner, then grade in a separate sandbox.
Persist normalized results, lifecycle events, a readable review and submission
diffs. Private evaluation files never enter the learner build context.

Network restrictions apply from sandbox creation. Phase-only switching failed a
real Modal TCP probe and is not used by Sprint. A model harness needing an API
requires an explicit transport allowlist and preinstalled tools when appropriate.

## Remaining integrations

Per-action workspace exports; enforceable action and
subagent budgets; screened download services; RL token/log-probability export.
These are distinct from the verified initial lifecycle.

Harbor internals may change substantially to support this contract. End users
should not need Harbor job models, Compose templates or provider scripts to
author an ordinary environment.


## Mid-task state recovery

`Experiment.start()` exposes an operator-controlled live run. `run.checkpoint(name)`
saves declared mutable directories, native agent history, worker files, request
lineage and remaining time. Restore starts fresh processes in fresh containers;
it does not preserve RAM, live GPU allocations or network connections.

Snapshots have complete manifests and content-addressed file chunks. Unchanged
chunks are shared across snapshots; `Checkpoint.diff()` reports changed paths.
File hashes are checked before restore. The compiled task and validation release
are retained, and restore requires the same experiment definition.

Capture temporarily pauses learner writes and waits for running broker requests
to finish. It publishes a manifest only after exports succeed, then resumes the
original branch. Call `stop()` to suspend that branch. Capture time consumes the
budget. Applications must save optimizer, scheduler and RNG state to files when
those are needed for recovery. Interrupted commands require inspection on resume.

See [checkpoint usage](checkpoints.md). Checkpoints are private operator data and
may include credentials from native harness state. Do not commit them to GitHub.
