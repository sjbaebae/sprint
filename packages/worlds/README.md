# Worlds

Supply a task and evaluation. Reuse the world and execution infrastructure.

```python
from pathlib import Path
from worlds import Agent, Experiment, Task, Validation

experiment = Experiment(
    task=Task("Improve the supplied implementation. Write the result to submission/."),
    validation=Validation(bundle=Path("validation")),
    agent=Agent(model="gpt-6-astra", harness="codex"),
)
result = await experiment.run(directory=Path(".worlds/trial"))
```

An evaluation bundle needs only `evaluate.py`:

```python
def verify(context):
    # context.submission: collected learner artifact
    # context.private: trusted evaluation files
    return {"score": 0.75, "metrics": {"accuracy": 0.75}}
```

This package lives in our [Harbor fork](https://github.com/sjbaebae/harbor/tree/worlds).
The initial backend uses the Harbor version in this uv workspace. The public objects do not import Harbor. Install the backend only
when compiling or executing tasks:

```sh
uv sync --package research-worlds --extra harbor
uv run --package research-worlds --extra harbor pytest packages/worlds/tests
uv run --package research-worlds --extra harbor python packages/worlds/examples/answer/preflight.py
```

## Independent pieces

| Object | Responsibility |
|---|---|
| `World` | Shared workspace, sandbox and tooling |
| `Sandbox` | Image, Python packages, network and CPU/GPU resources |
| `Task` | Objective and final artifact location |
| `Validation` | Private verifier, optional rubric judge and disqualification checks |
| `Agent` | Solver model, harness and harness-specific options |
| `Experiment` | Combine these definitions and run independent trials |
| `StateTracker` | Timestamped file manifests and text diffs on operator-owned workspace exports |

World and validation images are built separately. Evaluation code/private data
are never copied into the learner build context. `compile()` emits standard Harbor
tasks for inspection; it does not provision resources. `run()` uses Harbor's own
job lifecycle, agent adapters and trajectory exports.

## Model-based grading and disqualification

Use `Validation(bundle=..., rubric="rubric.md", weights=(0.8, 0.2))` to combine
`verify(context)` with `judge(context, rubric_text)`. Both return a normalized
`score` plus optional metrics and evidence. The author supplies the model call;
provider credentials are not implicitly mounted or forwarded to either sandbox.

Optional `disqualify(context)` returns:

```python
{"confirmed": [{"id": "protected_data_use", "evidence": ["trusted matching record"]}],
 "suspected": []}
```

Confirmed violations receive zero reward. Suspicions and grader errors remain
unscored; they are not silently treated as model failures. This is the interface
for exact-match/n-gram screening; a domain-specific contamination implementation
is not bundled yet. Never execute submitted code in the trusted grader process.

## Inspect file changes

```python
from worlds import StateTracker

tracker = StateTracker("exported-workspace", "operator-records")
before = tracker.capture("before", turn=1)
# Run an action, then export its workspace.
after = tracker.capture("after", turn=1)
changes = tracker.diff(before, after)
```

This utility records hashes, bounded text, symlinks without following them, and
explicit omissions for large/special files. It is for quiescent operator-owned
exports, not atomic capture of live hostile filesystems. Stores must be outside
the learner workspace. Never commit exported secrets or private evaluation data.

## Current boundary

This is the first authoring implementation, not a production release. Local
contract tests cover compilation, isolation of build contexts, grading outcomes
and file diffs (24 tests). Real Docker trials also verified reward 0 for an empty
submission and reward 1 for the reference solution, using separate verifier images.
The toy preflight explicitly enables networking: this Docker Desktop host rejects
offline enforcement. Default environments stay offline and fail closed when the
provider cannot enforce that policy. Runpod validation is in progress.

See [architecture.md](architecture.md) for the simplification contract.

Per-action workspace exports, immutable live event collection, a unified viewer,
hard action/subagent limits, screened HF downloads, training-job/checkpoint
services and RL token/log-probability export are not yet connected here. Native
Harbor logs are not claimed to be tamper-proof. Unsupported guarantees are
documented rather than exposed as settings that do nothing.
