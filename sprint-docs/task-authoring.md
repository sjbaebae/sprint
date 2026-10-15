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

Sprint has `TensorModelSession` for isolated model execution. A general-purpose
software submission session with this same boundary is not yet a built-in helper.
Until it exists, the author must implement that execution boundary explicitly;
do not represent a plain `pytest` invocation beside hidden tests as hardened grading.

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
