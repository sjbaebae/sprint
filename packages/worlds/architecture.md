# Architecture contract

Authors supply the task and validation. Reuse a world and select an agent.

- **World:** sandbox image, workspace assets, network policy, tooling and resources.
- **Task:** objective, instructions and submission location.
- **Validation:** private verifier, rubric judge, evidence and violation handling.
- **Agent:** model and harness; independent of the task and its target model.
- **Execution:** provisioning, budgets, artifact transfer, teardown and result persistence.
- **State:** timestamped events, workspace snapshots and file differences.

Worlds, tasks and validation must be reusable independently. Provider and harness
configuration belongs behind adapters. Authors should not need Harbor job models,
Docker Compose templates or infrastructure scripts for ordinary environments.

## Current implementation

The small Python API compiles into Harbor tasks and executes the native lifecycle.
Learner and validation images have separate build contexts. Grading failures and
suspected violations remain unscored. Snapshot exports have explicit omissions.

## Next integration work

Return a stable Worlds result object rather than a native Harbor result. Connect
workspace exports and timestamped events to action boundaries. Build one readable
run view from those records. Implement capabilities for action budgets and agent
restrictions, rejecting unsupported policies. Keep GPU job services separate from
task definitions. Add provider preflights for isolation and requested network policy.

These are implementation commitments, not claims that the current release already
supports them. Harbor internals can change substantially to satisfy this contract;
backward compatibility is confined to the backend boundary.
