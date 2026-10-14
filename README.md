# Sprint

Build agent environments by supplying a task, its evaluation, and the constraints. Sprint runs the agent in a sandbox, attaches resources such as GPU workers, grades the final submission privately, and saves a readable transcript.

One distribution: **`sprint`**. One Python namespace: **`sprint`**.

## 1. Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. Install from GitHub; Sprint is not published to PyPI.

```sh
uv init my-environment
cd my-environment
uv add "sprint[modal] @ git+https://github.com/sjbaebae/sprint.git@worlds"
uv run modal setup
```

Use `sprint` without `[modal]` for local Docker execution. Pin a reviewed commit instead of `@worlds` for reproducible deployments. Docker must be running for the Docker backend; networking restrictions also require backend support and fail closed when unsupported.

The runner stays on the machine calling `run()`. To choose a Runpod CPU host explicitly, pass `host=Host(...)`; sandboxes and workers have separate placement. See [host and resource placement](sprint-docs/hosts.md).

For a prepared dedicated Linux Pod without nested containers, the
[allocated-host backend](sprint-docs/allocated-host.md) runs the learner directly
and keeps validation and GPU workers on separately selected container backends.

### Create an environment

After installing Sprint into your uv project:

```sh
uv run sprint env init demo
uv run sprint env compile demo                   # Inspect; no resources
uv run sprint env run demo --backend modal
```

Edit the task in `demo/environment.py` and scoring in
`demo/validation/evaluate.py`. Those are the only required authored files.
The scaffold includes a single-agent Codex configuration, a one-hour budget,
full learner internet and private performance-only evaluation. Sign in with
`codex login` before launching. Resource attachments and research judging are
optional additions. Existing directories are never overwritten.
For independent trials, add `--trials 6 --parallel 6` to `env run`.

## Edit the task and evaluation

The generated `environment.py` defines `experiment = Experiment(...)`.
Change `Task(...)` to your objective and submission requirements;
`Budget(seconds=...)` controls wall time. The generated agent has a 200-action
limit, disabled subagents and full learner internet. Adjust these settings in
that file. Compilation imports your definition, so keep provisioning out of
module-level code.

`validation/evaluate.py` supplies private scoring:

```python
def verify(context):
    answer = context.submission / "answer.txt"
    correct = answer.is_file() and answer.read_text().strip() == "42"
    return {"score": float(correct), "metrics": {"correct": correct}}
```

Scores are between zero and one. Place hidden answers and test data in
`validation/`; Sprint copies that bundle only into the separate verifier.
Public starting files go in `Env(files=Path("workspace"))`.

Launch six independent trials with:

```sh
uv run sprint env run demo --backend modal --trials 6 --parallel 6
```

The command prints rewards and the records directory. Provider setup and harness
authentication are required on the launching host. See [manual setup and API-key
authentication](sprint-docs/manual-setup.md) for custom configurations.

## Inspect results

Open `review.html` in the printed records directory for the transcript, turn timestamps, tool calls, worker feedback, and grading. `result.json` and `events.jsonl` contain structured records. Submission snapshots and diffs are retained per trial. For mid-task state recovery, use [`Experiment.start()` and checkpoints](sprint-docs/checkpoints.md). Missing reasoning summaries or timestamps are displayed as missing; requesting summaries does not guarantee the provider returns them.

## Attach GPU workers or data

```python
from pathlib import Path
from sprint import Env, Files, Resources, Sandbox, Worker

env = Env(
    sandbox=Sandbox(
        internet="allowlist",
        allowed_hosts=("chatgpt.com", "auth.openai.com", "api.openai.com"),
    ),
    attachments={
        "data": Files(Path("datasets")),
        "train": Worker(resources=Resources(gpu="L4"), seconds=600),
    },
)
```

Pass this as `Experiment(env=env, ...)`. Use a preinstalled harness image or the `Sandbox.setup` from the full example above.

`Files` mounts read-only public data at `/resources/data` in the learner and workers. A named worker is created on first use and retains its workspace until the trial ends. Configure its sandbox packages/image for your training stack. The learner submits Python scripts using:

```sh
sprint-resource list
sprint-resource upload train config.json config.json --wait 60
sprint-resource run train experiment.py --wait 60
sprint-resource status REQUEST_ID --wait 60
sprint-resource collect REQUEST_ID checkpoint.pt --wait 60
```

For interactive GPU experimentation, set `Worker(..., shell=True)` and use `sprint-resource exec train 'COMMAND' --wait 60`. Successful shell calls preserve background processes; Python jobs clean up processes after each job. Both retain files until the worker closes.

The broker returns execution output and checkpoint collection feedback without giving the learner provider credentials. See [resource access](sprint-docs/resource-access.md) for limits and lifecycle details.

### Direct learner SSH on Modal

Use an attached network-enabled shell worker:

```python
"train": Worker(
    sandbox=Sandbox(internet="full"),
    resources=Resources(gpu="L4"),
    backend="modal", shell=True, lifetime_seconds=3600,
)
```

The learner requests its connection and uses ordinary SSH/SCP:

```sh
sprint-resource connect train --wait 60
ssh -F /resources/control/connections/train/config gpu 'COMMAND'
scp -F /resources/control/connections/train/config LOCAL_FILE gpu:/app/REMOTE_FILE
```

Install an SSH client in the learner image and permit network access to the worker.
Sprint owns provisioning, trial-scoped keys, pinned host configuration and cleanup.
Checkpoint restore refreshes the same config path before resuming the agent.
Worker files survive; processes and GPU memory must be restarted from saved files.
Direct SSH commands appear in the agent transcript, rather than broker execution
records. For custom external-host integration, see [resource access](sprint-docs/resource-access.md).

## Evaluate a submitted model

`TensorModelSession` runs a complete model package in a separate network-blocked Modal sandbox, without labels, scorer files or operator credentials. The package contains `model.json` (`{"format":"tensor-model-v1","entrypoint":"model.py"}`), an entrypoint exporting `load_model(directory, device)`, and all required local assets. The returned callable accepts `input_ids` and `attention_mask` and returns finite `[batch, vocabulary]` logits for the final position. Internal architecture and inference are model-owned.

```python
from sprint import TensorModelSession

async with TensorModelSession(
    package_path, directory=private_scratch, vocabulary_size=vocabulary_size,
    seconds=600,
) as model:
    values = await model.predict(input_id_rows, output_token_ids)
# Compare numeric outputs with hidden labels here, outside the model worker.
```

Use private temporary storage for `private_scratch`: it contains input tensors. Package import must only inspect structure and hashes; execute package code inside the isolated session. The calling host needs outbound Modal/blob transport for package uploads; the model sandbox stays network-blocked. A live Runpod → Modal check reproduced the pinned model baseline through this interface and verified cleanup.

## Add judge scoring and violations

```python
validation = Validation(
    bundle=Path("validation"),
    rubric="rubric.md",
    weights=(0.8, 0.2),  # verifier, judge
)
```

Implement `judge(context, rubric)` alongside `verify(context)`. Both return `{"score": ...}` in `[0, 1]`, with optional metrics and evidence. The author supplies the judge model call and its authentication; a rubric file alone does not call a model. `context.records` exposes collected agent records, including operator worker records under `sprint-resources`.

Optional `disqualify(context)` returns `{"confirmed": [...], "suspected": [...]}`; every violation needs an `id` and `evidence`. Confirmed violations yield zero reward. Suspected violations and grader errors quarantine the result without a final reward. Final reward is the weighted sum of verifier and judge scores.

### Performance-only variants

The judge is optional. To score measured performance while retaining cheating
checks, use:

```python
validation = Validation(bundle=Path("validation"), weights=(1.0, 0.0))
```

This is also the default. Sprint still calls `disqualify(context)` before
`verify(context)`, but never calls `judge`. No rubric or judge credentials are
needed. Supply environment-specific contamination and tampering checks in
`disqualify`; disabling the judge does not create an automatic cheating detector.
Confirmed violations score zero; unresolved suspicions remain quarantined.

When remote evaluation and judging have already completed, use `sprint.validation.combine_components(verifier, judge, weights=(0.8, 0.2))` to apply the same normalized-score and weighting rules. Resolve integrity dispositions before combining; missing or malformed required components raise an error rather than receiving a fallback score.

## Develop Sprint

```sh
git clone --branch worlds https://github.com/sjbaebae/sprint.git
cd sprint
uv sync --no-default-groups --extra modal
uv run --no-default-groups --with pytest --with pytest-asyncio pytest tests/sprint -q
```

- [Authoring details](sprint-docs/README.md)
- [Architecture](sprint-docs/architecture.md)
- [Checkpoint and recovery](sprint-docs/checkpoints.md)
- [Read-only judge evidence](sprint-docs/evidence.md)
- [Runnable examples and preflights](examples/sprint)
- [Implementation](src/sprint)

Sprint originated from Harbor 0.24.0 (`dc933be707feafbb9efcdf092cb8a0e391332617`). Its retained execution engine, providers, and agent adapters now live inside the Sprint namespace. Upstream documentation and citation metadata remain in [reference/harbor](reference/harbor). [Apache-2.0 license](LICENSE).
