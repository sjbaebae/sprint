# Manual setup and authentication

The [environment scaffold](../README.md#create-an-environment) supplies these settings. Use this reference when configuring images, harnesses or authentication directly.

## Private evaluation

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

Set `Validation(..., backend="modal")` or `backend="docker"` to place validation
independently; the default inherits the run backend.

The validation bundle is copied only into the separate verifier. Put hidden answers and test data here. Public workspace files belong in `Env(files=Path("workspace"))`; never place private evaluation data there.

## Define and run through Python

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

For an enforced action budget, select `harness="bounded-codex"` and set
`options={"max_turns": 200, ...}`. Sprint compiles its root-owned permissions
profile, disables subagent features and preserves spent actions on checkpoint
restore. Tool calls and agent messages count; reasoning summaries do not.
The learner is told its exact remaining budget and the live status file path.
The bounded adapter supports up to 200 actions and two hours per trial. Its managed
policy follows the [Codex requirements format](https://learn.chatgpt.com/docs/config-file/config-reference).

For repeated independent trials, use `trials=6, parallel=6` in `experiment.run(...)`. Use a new output directory for a new campaign. `experiment.compile(Path("compiled"))` writes the task for inspection without launching it.

### API-key billing: Codex or Claude Code

Set the appropriate key in the host environment or secret manager. Keep keys out of Python files, Git, and image build commands.

```python
codex = Agent(model="gpt-6-astra", harness="codex", auth="api-key")
claude = Agent(model="YOUR_CLAUDE_MODEL_ID", harness="claude-code", auth="api-key")
```

Codex uses `OPENAI_API_KEY`; Claude Code uses `ANTHROPIC_API_KEY`. `auth="api-key"` requires the key before provisioning and suppresses subscription authentication for that trial. Default `auth="auto"` retains the existing adapter authentication behavior, including the Codex sign-in setup above.

Choose the agent in `Experiment(agent=codex, ...)` or `Experiment(agent=claude, ...)`. For Codex API access allow `api.openai.com`; for Claude Code allow `api.anthropic.com`. Sprint installs the selected CLI when using the default image. Pin `Agent.options["version"]` for reproducible builds. With custom images, preinstall the CLI or allow the adapter's runtime installer network access. Use a model ID available to your API account. API-key billing is separate from subscription usage. See [Codex authentication](https://learn.chatgpt.com/docs/auth).

Credentials are injected into the harness at runtime and redacted from serialized agent configuration. They are accessible inside its sandbox; this is not a credential-isolating inference proxy. Worker jobs do not inherit harness credentials.


## Compiled harness policy

The scaffold uses bounded Codex with root-owned managed requirements that disable
built-in subagents. Compiled Claude Code environments install root-owned
`/etc/claude-code/managed-settings.json`, denying `Agent`, its older `Task` alias
and team messaging tools. Restricted-network environments also deny Claude's
hosted web tools. Verifier and worker images do not receive this learner policy.

These controls apply to the selected harness's built-in tools. They do not
prevent arbitrary programs or API clients launched through a permitted shell.
Network permissions and credential placement remain separate controls.

Claude documents the [Agent denial](https://code.claude.com/docs/en/sub-agents)
and [managed-settings precedence](https://code.claude.com/docs/en/settings).
