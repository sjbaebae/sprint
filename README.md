# Sprint

Author agent environments by defining the world, task, validation, and agent. Attach CPU/GPU workers and shared files as named resources.

## Install with uv

Python 3.12+ is required. Install from GitHub (the package is not published to PyPI):

```sh
uv init my-environment
cd my-environment
uv add "research-sprint @ git+https://github.com/sjbaebae/sprint.git@worlds"
# For Modal CPU/GPU workers:
uv add "research-sprint[modal] @ git+https://github.com/sjbaebae/sprint.git@worlds"
uv run python environment.py
```

For local development:

```sh
git clone --branch worlds https://github.com/sjbaebae/sprint.git
cd sprint
uv sync --no-default-groups
# Optional Modal support:
uv sync --no-default-groups --extra modal
uv run --no-default-groups python examples/sprint/answer/environment.py
```

The examples require provider authentication and a configured agent harness. See the [authoring guide](sprint-docs/README.md) for deployment settings. For reproducible deployments, replace `@worlds` with a reviewed commit hash.

## Define an environment

```python
from pathlib import Path
from sprint import Agent, Experiment, Task, Validation, World

experiment = Experiment(
    world=World(),
    task=Task("Write the answer to 6 × 7 in /app/submission/answer.txt."),
    validation=Validation(bundle=Path("validation")),
    agent=Agent(model="gpt-6-astra", harness="codex"),
)
```

Validation is private Python code: `verify(context)` supplies performance, optional `judge(context, rubric)` supplies research quality, and `disqualify(context)` handles violations. Each scoring component returns a score in [0, 1].

- [Authoring guide and runnable examples](sprint-docs/README.md)
- [Architecture and responsibilities](sprint-docs/architecture.md)
- [Resource access and isolation](sprint-docs/resource-access.md)
- [Implementation](src/sprint)
- [Tests](tests/sprint)

From this checkout, use `uv run --package research-sprint --no-default-groups python YOUR_ENVIRONMENT.py`. Add `--extra modal` for Modal execution.

## Relationship to Harbor

Sprint is a Harbor fork with a new Python authoring layer and a host-managed resource broker. It retains Harbor's agent adapters, sandbox providers, job/trial lifecycle, and trajectory format. Sprint adds named persistent workers, private structured scoring, submission diffs, and a unified review page. The execution engine remains Harbor.

The existing `src/harbor`, provider integrations, benchmark adapters, and upstream tests remain available for compatibility and future integration. They are not required reading for authoring a Sprint environment.

Based on Harbor 0.24.0, commit `dc933be707feafbb9efcdf092cb8a0e391332617`. Original project documentation and citation metadata are retained under [reference/harbor](reference/harbor). [Apache-2.0 license](LICENSE).
