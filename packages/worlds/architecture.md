# Architecture contract

Authors supply the task and validation. Reuse a world, attach resources and select
an agent. Provider configuration stays behind execution adapters.

| Layer | Owns |
|---|---|
| World | Public assets, tools and resource access |
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

The current release implements world/verifier capacity. Named remote attachments
are next; no placeholder configuration is presented as a working attachment.

## Implemented lifecycle

Compile an inspectable task, provision the learner, run its harness, collect the
artifact and agent records, stop the learner, then grade in a separate sandbox.
Persist normalized results, lifecycle events, a readable review and submission
diffs. Private evaluation files never enter the learner build context.

Network restrictions apply from sandbox creation. Phase-only switching failed a
real Modal TCP probe and is not used by Worlds. A model harness needing an API
requires an explicit transport allowlist and preinstalled tools when appropriate.

## Remaining integrations

Named remote resources; per-action workspace exports; enforceable action and
subagent budgets; screened download services; RL token/log-probability export.
These are distinct from the verified initial lifecycle.

Harbor internals may change substantially to support this contract. End users
should not need Harbor job models, Compose templates or provider scripts to
author an ordinary environment.
