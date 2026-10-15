# Author a useful task

A task is a bounded opportunity to improve an artifact, with an independent way
to measure the result. Start with a real failure or bottleneck, a reproducible
starting state, and a result someone would use.

## What the author supplies

| Input | Software example |
|---|---|
| Workspace | A small repository pinned to a revision, with dependencies |
| Objective | Fix incremental parsing when a document ends with an incomplete token |
| Submission | The modified source tree, with a documented entrypoint |
| Verification | Hidden regression cases plus existing behavior checks |
| Constraints | One hour, permitted tools, network policy and compute budget |

Use `uv run sprint env init parser-fix`. Edit `environment.py` and
`validation/evaluate.py`; put public starting files in `workspace/` and private
fixtures in `validation/`. See the [working API](../README.md#edit-the-task-and-evaluation).
There is no requirement for a task-specific broker, GPU service, model lock,
research rubric or separate runtime configuration file.

Sprint owns harness setup, trial isolation, budgets, resource lifecycle, traces,
submission collection, result records and logical-state checkpoint recovery.
The author owns the meaning and correctness of the evaluation. Recovery restores
saved files and conversation state; it does not restore live processes or GPU memory.

## Software evaluation

Evaluate behavior rather than the agent's explanation. A bug-fix task needs
regression coverage and checks against breaking existing behavior. An optimization
task needs correctness gates and repeated measurements under fixed resources.
An integration task needs a realistic contract fixture or isolated service.

Do not import submitted Python or execute submitted scripts inside the trusted
scorer. A separate verifier keeps evaluation away from the learner, but running
untrusted code in that verifier would expose its hidden tests and scoring files.
Run the implementation in another restricted execution sandbox; retain hidden
inputs, expected outputs and score computation outside it. Bound execution time,
output size and resources. Treat returned results as untrusted data.

Use `SoftwareSession` for command-line software and `TensorModelSession` for models.
These are operator-side helpers: provider credentials stay outside the verifier
and submitted program. A private `Validation.prepare` callback can use them before
the isolated scorer reads its prepared measurements; see
[operator preparation](resource-access.md).

```python
from sprint import SoftwareSession

async with SoftwareSession(
    submission_directory, directory=private_records, seconds=120,
) as program:
    result = await program.run(["python", "main.py"], stdin=b'{"values":[6,7]}')
# Validate output shape and compare with expected answers outside the sandbox.
```

The default is an offline Modal CPU worker. Specify a trusted `Sandbox` to install
dependencies during image build, or `backend="docker"` on a compatible host.
The directory contains at most 1000 regular files and 1 GiB; links and special
files are rejected. Invoke interpreters explicitly rather than relying on retained
executable permissions. stdin is bounded to 1 MiB, and stdout/stderr are each
bounded to 1 MiB with truncation flags in the returned feedback. Reject truncation
when complete output is needed for grading.

Only supplied inputs cross the boundary; do not include expected answers or
private test code in the submission. Files persist between calls in one session;
use fresh sessions when tests require independent state. The context closes its
worker, including on exceptions. Records can contain protected input data and
must remain private. Do not represent a plain `pytest` invocation beside hidden
tests as hardened grading.

## What makes the task useful to a lab

- A real starting failure and a known feasible solution under the budget.
- A verifier that rewards the intended improvement and rejects shortcuts.
- Hidden cases sufficiently different from public examples to measure transfer.
- A pinned release, baseline and resource budget for comparable trials.
- Readable traces and attributable failures for debugging and training analysis.

Keep one task focused. Reuse its execution environment for several objectives and
hidden test sets. Add a judge only for qualities that deterministic checks cannot
measure; confirmed cheating, suspected cheating and infrastructure failure remain
distinct outcomes. A model's failure should be interpretable without guessing
whether the environment itself worked.

For RL use, trials currently provide traces and rewards. Policy optimization and
training-ready token/log-probability export are not integrated.
