# Architecture contract

Authors supply the task and validation. Reuse an environment, attach resources and select
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

The host runs orchestration. A learner, attached worker and verifier have independent
resource requests. GPU capacity is explicit; it is never inherited by the
verifier. Multiple named attachments should grant explicit operations, such as
executing a job, reading a dataset or accessing a checkpoint. Capacity and access
permissions are separate. Provider credentials stay on the operator side.

Choose either brokered workers (`Env.attachments`) or ordinary learner SSH/SCP
(`ModalWorker`). Both keep provider credentials outside the learner. Sprint owns
provisioning, deadlines, observations, artifact collection and teardown; learner
code owns its training loop and processes. Broker workers close before verification.
A submitted model package runs in a fresh network-blocked worker, separately from
the protected scorer.

## Implemented lifecycle

Compile an inspectable task, provision the learner, run its harness, collect the
artifact and agent records, stop the learner, then grade in a separate sandbox.
Persist normalized results, lifecycle events, a readable review and submission
diffs. Private evaluation files never enter the learner build context.

Network restrictions apply from sandbox creation. Phase-only switching failed a
real Modal TCP probe and is not used by Sprint. A model harness needing an API
requires an explicit transport allowlist and preinstalled tools when appropriate.

## Remaining integrations

The direct SSH lifecycle is currently an operator API, separate from
`Env.attachments`. Broker request and spending limits do not govern direct SSH;
its resource lifetime bounds access. Automatic per-action workspace exports and
RL token/log-probability export remain unfinished.

The bounded Codex adapter enforces action and single-agent limits; ordinary
harness adapters retain their own capability limits. Environment-specific model
evaluation and contamination policy belong in validation, not provider adapters.


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

## Host placement

The caller is the default operator host. `Experiment.run(host=Host(...))` stages
the release over SSH and runs the scheduler/broker on that existing host. Sandbox
placement still follows `backend`; `Worker.backend` overrides individual workers.
See [hosts](hosts.md) for Runpod CPU → Modal execution and current boundaries.
