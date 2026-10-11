"""Small live Codex trial; auth is injected at runtime by Harbor, never built in."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from sprint import Agent, Budget, Experiment, Sandbox, Task, Validation, Worker, Env


async def main():
    # Existing operator sign-in; never copy auth into an authored task or image.
    auth = Path.home() / ".codex/auth.json"
    if not auth.is_file():
        raise RuntimeError("Sign into Codex locally before this preflight")
    os.environ["CODEX_AUTH_JSON_PATH"] = str(auth)
    root = Path(".sprint/codex-preflight") / uuid4().hex
    root.mkdir(parents=True)
    private = root / "private"
    private.mkdir()
    (private / "evaluate.py").write_text(
        'def verify(context):\n    p = context.submission / "answer.txt"\n    return {"score": float(p.is_file() and p.read_text().strip() == "42")}\n'
    )
    sandbox = Sandbox(
        setup=(
            "apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*",
            "npm install -g @openai/codex@0.162.1",
        ),
        internet="allowlist",
        allowed_hosts=("chatgpt.com", "auth.openai.com", "api.openai.com"),
    )
    experiment = Experiment(
        Task(
            "Use sprint-resource to run Python on the worker named compute. Have it write answer.txt containing the sum of integers 1 through 9 minus 3. Collect answer.txt from that completed request and copy it to /app/submission/answer.txt. Explain briefly why you chose each action. No web search, no subagents. Do not inspect credentials. Finish after checking the answer exists."
        ),
        Validation(private),
        Agent(
            "gpt-6-astra",
            options={
                "version": "0.162.1",
                "reasoning_effort": "medium",
                "reasoning_summary": "detailed",
                "web_search": "disabled",
                "config": {
                    "features": {"multi_agent": False},
                    "agents": {"max_threads": 1},
                },
            },
        ),
        Env(sandbox=sandbox, attachments={"compute": Worker()}),
        Budget(seconds=300),
    )
    result = await experiment.run(directory=root / "run", backend="modal")
    print(root, result.rewards)
    if result.rewards != [1.0]:
        raise RuntimeError(
            "live Codex preflight failed; inspect retained trial records"
        )


if __name__ == "__main__":
    asyncio.run(main())
