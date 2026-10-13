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

## 2. Write private evaluation

Create this layout:

```text
my-environment/
  environment.py
  validation/
    evaluate.py
```

In `validation/evaluate.py`:

```python
def verify(context):
    answer = context.submission / "answer.txt"
    correct = answer.is_file() and answer.read_text().strip() == "42"
    return {"score": float(correct), "metrics": {"correct": correct}}
```

The validation bundle is copied only into the separate verifier. Put hidden answers and test data here. Public workspace files belong in `Env(files=Path("workspace"))`; never place private evaluation data there.

## 3. Define and run the environment

For Codex, sign in locally using `codex login`. Sprint injects the existing authentication at runtime; it is not baked into the image.

In `environment.py`:

```python
import asyncio
import os
from pathlib import Path

from sprint import Agent, Budget, Experiment, Sandbox, Task, Validation, Env

async def main():
    auth = Path.home() / ".codex/auth.json"
    if not auth.is_file():
        raise RuntimeError("Run codex login first")
    os.environ["CODEX_AUTH_JSON_PATH"] = str(auth)

    experiment = Experiment(
        env=Env(sandbox=Sandbox(
            setup=(
                "apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*",
                "npm install -g @openai/codex@0.162.1",
            ),
            internet="allowlist",
            allowed_hosts=("chatgpt.com", "auth.openai.com", "api.openai.com"),
        )),
        task=Task("Compute 6 × 7. Write /app/submission/answer.txt. Explain your actions briefly."),
        validation=Validation(bundle=Path("validation")),
        agent=Agent(model="gpt-6-astra", harness="codex", options={
            "version": "0.162.1",
            "reasoning_effort": "medium",
            "reasoning_summary": "detailed",
            "web_search": "disabled",
            "config": {
                "features": {"multi_agent": False},
                "agents": {"max_threads": 1},
            },
        }),
        budget=Budget(seconds=300),
    )
    result = await experiment.run(directory=Path(".sprint/answer"), backend="modal")
    print(result.rewards)

if __name__ == "__main__":
    asyncio.run(main())
```

```sh
uv run python environment.py
```

The host orchestrates the trial; Modal runs its sandboxes. `Sandbox.setup` runs during image construction. Runtime networking is configured separately. The allowlist applies to every process in the learner sandbox, including the harness. Workers and validation default to no outbound internet. Harness options are adapter-specific; wall time is enforced by `Budget`, while action limits require harness support.

For repeated independent trials, use `trials=6, parallel=6` in `experiment.run(...)`. Use a new output directory for a new campaign. `experiment.compile(Path("compiled"))` writes the task for inspection without launching it.

### API-key billing: Codex or Claude Code

Set the appropriate key in the host environment or secret manager. Keep keys out of Python files, Git, and image build commands.

```python
codex = Agent(model="gpt-6-astra", harness="codex", auth="api-key")
claude = Agent(model="YOUR_CLAUDE_MODEL_ID", harness="claude-code", auth="api-key")
```

Codex uses `OPENAI_API_KEY`; Claude Code uses `ANTHROPIC_API_KEY`. `auth="api-key"` requires the key before provisioning and suppresses subscription authentication for that trial. Default `auth="auto"` retains the existing adapter authentication behavior, including the Codex sign-in setup above.

Choose the agent in `Experiment(agent=codex, ...)` or `Experiment(agent=claude, ...)`. For Codex API access allow `api.openai.com`; for Claude Code allow `api.anthropic.com`. Preinstall the corresponding CLI in the image (`@openai/codex` or `@anthropic-ai/claude-code`) or provide build-time installation in `Sandbox.setup`. Use a model ID available to your API account. API-key billing is separate from subscription usage. See [Codex authentication](https://learn.chatgpt.com/docs/auth).

Credentials are injected into the harness at runtime and redacted from serialized agent configuration. They are accessible inside its sandbox; this is not a credential-isolating inference proxy. Worker jobs do not inherit harness credentials.

## 4. Inspect results

Open `.sprint/answer/review.html` for the transcript, turn timestamps, tool calls, worker feedback, and grading. `result.json` and `events.jsonl` contain structured records. Submission snapshots and diffs are retained per trial. For mid-task state recovery, use [`Experiment.start()` and checkpoints](sprint-docs/checkpoints.md). Missing reasoning summaries or timestamps are displayed as missing; requesting summaries does not guarantee the provider returns them.

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

An operator can give the learner ordinary SSH/SCP access instead of routing commands through the resource broker:

```python
from sprint import Resources, Sandbox, Worker
from sprint.learner_ssh import create_identity, publish_connection
from sprint.workers import WorkerSession

identity = private_state / "learner-key"
public_key = create_identity(identity)
async with WorkerSession(
    Worker(
        sandbox=Sandbox(internet="full"), resources=Resources(gpu="L4"),
        shell=True, lifetime_seconds=3600,
    ),
    backend="modal", directory=private_state / "worker",
    learner_ssh_public_key=public_key,
) as session:
    await session.start()
    config = publish_connection(
        session.learner_ssh_endpoint, identity, public_connection_directory,
        uid=1000, gid=1000,
    )
    # Give the learner this path and keep the session open for the trial:
    # ssh -F CONFIG gpu 'COMMAND'
    # scp -F CONFIG LOCAL_FILE gpu:/app/REMOTE_FILE
```

Run this setup as the operator. `private_state` must be private; the parent of `public_connection_directory` must be learner-readable and operator-owned. The latter contains a learner-owned key and an operator-owned config with a pinned host key. It grants only the sandbox's `agent` account; root login and forwarding are disabled. Preinstall OpenSSH server in the worker image for network-blocked workers. Provider credentials stay on the operator host. Direct SSH does not create broker command records: retain the agent transcript and configure workspace observations when needed. Learner SSH/SCP and process control have been checked on a live Modal L4.

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
- [Runnable examples and preflights](examples/sprint)
- [Implementation](src/sprint)

Sprint originated from Harbor 0.24.0 (`dc933be707feafbb9efcdf092cb8a0e391332617`). Its retained execution engine, providers, and agent adapters now live inside the Sprint namespace. Upstream documentation and citation metadata remain in [reference/harbor](reference/harbor). [Apache-2.0 license](LICENSE).
