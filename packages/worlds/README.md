# Worlds

Define the objective and evaluation. Reuse the world, resources and agent harness.
This library lives in the [Harbor fork](https://github.com/sjbaebae/harbor/tree/worlds).

```python
from pathlib import Path
from worlds import Agent, Experiment, Resources, Task, Validation, World

experiment = Experiment(
    task=Task("Improve the supplied implementation. Put the result in submission/."),
    validation=Validation(bundle=Path("validation")),
    world=World(files=Path("workspace"), resources=Resources(gpu="L4")),
    agent=Agent(model="gpt-6-astra", harness="codex"),
)
result = await experiment.run(directory=Path(".worlds/trial"), backend="modal")
print(result.rewards)
# .worlds/trial/review.html contains results, evidence and recorded tool calls.
```

The example expresses the configuration; a live model harness also needs provider
authentication and permitted API transport. Offline worlds deny all outbound access,
including model API traffic. Preinstall tools in the image for offline execution.
Use an explicit hostname allowlist when a harness needs API access. Credentials
are never included in the public workspace or evaluation build context by Worlds.

An evaluation bundle needs one file, `evaluate.py`:

```python
def verify(context):
    # context.submission: final artifact
    # context.private: private evaluation files
    # context.records: collected agent logs/trajectory, untrusted evidence
    return {"score": 0.75, "metrics": {"accuracy": 0.75}}
```

## Run the verified example

From the fork root, using existing Modal authentication:

```sh
uv sync --package research-worlds --extra modal
uv run --package research-worlds --extra modal python packages/worlds/examples/answer/preflight.py --backend modal
# Optional hardware preflight:
uv run --package research-worlds --extra modal python packages/worlds/examples/answer/preflight.py --backend modal --gpu L4
```

The preflight checks an empty submission (reward 0) and a reference solution
(reward 1), public workspace availability, absence of private answers in the
learner container, and a blocked outbound TCP connection. The GPU option checks
`nvidia-smi` too. Every invocation uses a fresh directory and tears down workers.
It makes no frontier model calls.

For Docker Desktop, explicitly enable networking for the toy example:

```sh
uv run --package research-worlds --extra harbor python packages/worlds/examples/answer/preflight.py --internet full
```

Unsupported network policies fail closed. Never use this public-network toy
configuration as evidence that an offline environment was tested.

## Independent pieces

| Object | Responsibility |
|---|---|
| `World` | Public workspace and execution setting |
| `Sandbox` | Image, Python packages and network policy |
| `Resources` | CPU, RAM, GPU type and count for one worker role |
| `Task` | Objective and submission location |
| `Validation` | Private verifier, optional rubric judge and disqualification |
| `Agent` | Model, harness and harness-specific options |
| `Experiment` | Combine definitions; compile or execute independent trials |
| `RunResult` | Stable results and review records |

The host calls `run()`; resource requests apply to the provisioned workers.
World and validation requests are independent, so a GPU experiment can use a
CPU-only verifier. Named remote resource attachments remain separate integration
work; they are not represented by silent, unused settings.

## Grading

Use `Validation(bundle=..., rubric="rubric.md", weights=(0.8, 0.2))` to combine
`verify(context)` and `judge(context, rubric_text)`. Both return a normalized
`score` with optional metrics and evidence. The author supplies the judge model
call and its credential transport. Traces are evidence, never judge instructions.

Optional `disqualify(context)` returns:

```python
{"confirmed": [{"id": "protected_data_use", "evidence": ["trusted matching record"]}],
 "suspected": []}
```

Confirmed violations score zero. Suspicions and grader failures remain unscored.
Submission links and special files are rejected before grading. Contamination
matching is task-specific and must be supplied by the author. Never execute
learner code in the trusted grader process.

## Records

Each run retains:

- `result.json`: rewards, metrics, violations, errors and record paths.
- `events.jsonl`: operator-observed lifecycle timestamps.
- `review.html`: a vertical offline transcript with escaped commands and output.
- `runs/`: native provider/harness records, artifacts and evaluation output.
- Per-trial `state/`: before/after submission manifests and text diffs.

Snapshots cover the declared submission tree, not the whole live filesystem.
Large/special files have explicit omissions. Missing trajectory timestamps or
reasoning are reported as missing; lifecycle times are not invented turn times.
An agent reaching its time limit can still receive a valid final-artifact grade.
Infrastructure failures remain distinct from reward zero. Records can contain
sensitive material; runtime directories are ignored by Git.

`StateTracker` also supports explicit snapshots of quiescent operator-owned
workspace exports. It is not an atomic snapshot of a live hostile filesystem.

## Development

```sh
uv run --package research-worlds --extra harbor pytest packages/worlds/tests -q
```

Current checks: 35 library tests; 247 focused Harbor regression tests; real CPU
and L4 Modal preflights with offline networking and separate grading containers.
Runpod hosts the checkout at `/workspace/harbor-worlds`; its current CPU pod lacks
Docker and rejects namespace creation. It can orchestrate remote workers, but
nested sandbox execution on that pod has not been validated.

Per-action snapshots, named remote resource attachments, enforced action/subagent
limits, screened dataset downloads and RL token/log-probability export are not
connected in this initial library. Native harness logs are not tamper-proof.
See [architecture.md](architecture.md) for the boundaries and next steps.

### Attach public data

```python
from pathlib import Path
from worlds import Files, World

world = World(attachments={"data": Files(Path("datasets"))})
```

The agent reads `/resources/data`. Contents are copied into the learner image,
owned by root and read-only for the agent. Attachments are excluded from the
private verifier image. Names cannot contain paths; symlinks, special files and
trees overlapping the private validation bundle are rejected. This is a copied
image attachment, so very large datasets should use a future storage adapter.
Named worker job submission is not implemented yet.
