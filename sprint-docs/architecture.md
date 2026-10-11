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


## Full-run checkpoints: required contract, not yet implemented

A resumable checkpoint must restore the mid-task RL run, rather than start a new
trial with copied files. Capture CPU/process memory, registers and process trees;
all writable disks and resource filesystems; native agent conversation and harness
state; queue entries, completed/pending request IDs and feedback; budgets, action
counters, RNG state, and pinned environment versions. Running GPU resources require
device-state checkpoint support. Never silently downgrade to filesystem-only state.

Incremental storage is an optimization of this complete checkpoint: retain an
immutable base plus disk blocks and dirty memory pages. Restore materializes the
same complete state. Human-readable file diffs are an inspection view, not the
state representation and not sufficient for restoration.

Freeze the participating runtimes and host scheduler into one consistent cut.
Publish the checkpoint only after every component is durable. On failure, record
an incomplete checkpoint and resume or safely stop the original run; never expose
a partially successful checkpoint as restorable. Restore into an isolated branch,
with the saved remaining budget, and retain its parent checkpoint ID.

External API services and irreversible external side effects are outside the
snapshot. Record in-flight operations and their recovery policy; never assume an
inference request, network connection, or remote write can be rolled back. Runtime
snapshots may contain credentials and private data and require private storage.

Current state: submission manifests/text diffs and worker artifact collection are
implemented. Full-run suspend/restore is not. Modal filesystem snapshots alone do
not meet this contract; its memory-snapshot limitations exclude the current live
GPU workflow. A process/VM backend with appropriate device support is required.
