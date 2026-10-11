# Sprint

Author a task and its evaluation. Reuse a sandbox, attach resources and choose an
agent harness. Sprint is one root package. See [installation](../README.md#install-with-uv).

```python
from pathlib import Path
from sprint import Agent, Experiment, Files, Resources, Task, Validation, Worker, Env

experiment = Experiment(
    task=Task("Improve the supplied implementation."),
    env=Env(
        files=Path("workspace"),
        attachments={
            "data": Files(Path("datasets")),
            "train": Worker(resources=Resources(gpu="L4")),
        },
    ),
    validation=Validation(bundle=Path("validation")),
    agent=Agent(model="gpt-6-astra", harness="codex"),
)
result = await experiment.run(directory=Path(".sprint/trial"), backend="modal")
print(result.rewards)
```

The default sandbox denies outbound traffic. A live harness needs a preinstalled
image, authentication and an explicit model-API allowlist. This configuration
example does not supply those deployment settings. Provider credentials stay on
the host. Do not put secrets or protected evaluation files in public assets.

## Evaluation

`validation/evaluate.py` is the only required evaluation file:

```python
def verify(context):
    # submission: final artifact; private: validation bundle
    # records: collected agent logs, treated as untrusted evidence
    # records/sprint-resources: operator-supplied worker code and execution records
    return {"score": 0.75, "metrics": {"accuracy": 0.75}}
```

For rubric grading, supply `judge(context, rubric_text)` with the same return
shape and configure `Validation(..., rubric="rubric.md", weights=(0.8, 0.2))`.
The author supplies the judge model call and credential transport.

Optional `disqualify(context)` returns `{"confirmed": [{"id": "...", "evidence":
["..."]}], "suspected": []}`. Confirmed violations score zero; suspicions and
grader failures remain unscored. Links and special files are rejected before
grading. Dataset contamination checks are task-specific. Never execute learner
code in the trusted grader process.

## Resources

| Definition | Agent access |
|---|---|
| `Env(files=...)` | Writable `/app` workspace |
| `Files(path)` | Read-only `/resources/<name>`, copied into the learner image |
| `Worker(sandbox=..., resources=...)` | Separate sandbox for Python jobs |
| `Validation(resources=...)` | Private grading sandbox, inaccessible to agent |

`Resources` requests CPU, RAM and optional GPU. `Sandbox` sets image, packages and
network policy. Env, worker and verifier hardware/network settings are independent.
Read-only file attachments are available at the same paths in the learner and its
workers. Worker images must contain required packages.

Inside the learner:

```sh
sprint-resource list
sprint-resource run train experiment.py --wait 60
sprint-resource status REQUEST_ID --wait 60
sprint-resource collect REQUEST_ID checkpoint.pt --wait 60
```

`run` returns a request ID and actual exit code/stdout/stderr when completed.
`collect` copies one regular file beneath the worker's `/app`; copy its returned
read-only `artifact` path into your submission. Checkpoints persist between jobs.
The host serializes jobs, retains code/hashes/UTC events and destroys workers before
verification. Jobs have `Worker.seconds` deadlines and a total
`Worker.lifetime_seconds` limit. Modal also enforces a provider lifetime.

Collection rejects symlink paths, special files, concurrent changes and files over
1 GiB. Source payloads are limited to 1 MiB; there are at most 200 resource requests
per trial. These are resource-service limits, independent of harness action counts.
See [resource access](resource-access.md) for boundaries.

## Test the complete path

From the fork root with existing Modal authentication:

```sh
uv run --package sprint --extra modal --no-default-groups python examples/sprint/worker/preflight.py --backend modal --gpu L4
```

This oracle preflight checks an offline L4 worker, checkpoint reuse, collection,
cleanup and independent grading. It makes no frontier model calls. The simpler
[answer preflight](examples/answer/preflight.py) also checks empty/reference rewards
and absence of private answers in the learner.

For Docker Desktop, pass `--backend docker --internet full`. This host rejects the
requested no-network policy; public-network tests do not verify offline isolation.
Unsupported policies fail closed.

## Live Codex smoke test

With existing local Codex sign-in:

```sh
uv run --package sprint --extra modal --no-default-groups python examples/sprint/codex/preflight.py
```

This requests Astra, pins Codex 0.162.1 and injects existing authentication at runtime.
The learner allows only the configured OpenAI service hosts; the worker and verifier
remain offline. The allowlist applies to all processes in the learner, not just the
harness. This is a small live inference test, not an offline-harness guarantee.
The retained Astra smoke test passed with reward 1.0 and six tool calls. Recorded
agent explanations and timestamps were present; reasoning summaries were empty
despite requesting `detailed`, and the review page marks that absence explicitly.

`Sandbox.setup` contains trusted image-build shell commands for system tooling.
Do not put secrets in build commands. Tools are installed before learner execution;
restricted runtime networking is not used to install dependencies.

## Review a run

Open `<run-directory>/review.html`: vertical transcript, recorded turn timestamps,
commands/results, worker source/feedback, lifecycle times and final grading.
Machine-readable records remain in `result.json`, `events.jsonl`, `resources/`
and native `runs/`. Per-trial `state/` contains before/after submission manifests
and text diffs. Missing timestamps/reasoning are shown as missing. Malformed
transcripts are reported without changing an otherwise valid reward.

Snapshots cover the submission tree. `StateTracker` supports additional snapshots
of quiescent workspace exports. Live per-action filesystem snapshots, enforced
harness action/subagent limits, screened downloads and RL token/log-probability
export are not connected. Native harness logs are not tamper-proof. Runtime
records may contain sensitive content and are ignored by Git.

## Development

```sh
uv run --package sprint --with pytest --with pytest-asyncio --no-default-groups pytest tests/sprint -q
```

[Architecture](architecture.md) describes the boundaries. Runpod can host the
orchestrator; the current CPU Pod lacks Docker/namespace support, so its workers
must run remotely. The provider-backed preflights above were launched locally.
