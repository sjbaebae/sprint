"""Check native Codex conversation recovery on a fresh Modal container."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from sprint import Agent, Budget, Env, Experiment, Sandbox, Task, Validation


async def main(*, bounded=False):
    auth = Path(
        os.environ.get("CODEX_AUTH_JSON_PATH", Path.home() / ".codex/auth.json")
    )
    if not auth.is_file():
        raise RuntimeError("Sign into Codex before this preflight")
    os.environ["CODEX_AUTH_JSON_PATH"] = str(auth)
    root = Path(".sprint/checkpoint-codex") / uuid4().hex
    private = root / "private"
    private.mkdir(parents=True)
    (private / "evaluate.py").write_text(
        'def verify(context):\n    p = context.submission / "answer.txt"\n    return {"score": float(p.is_file() and p.read_text().strip() == "42")}\n'
    )
    experiment = Experiment(
        Task(
            "Test state recovery. If /app/ready is absent, explain your plan, write 19 to "
            "/app/number.txt, create /app/ready, and run sleep 120 in the foreground. "
            "If /app/ready already exists, skip that initialization and waiting. "
            "Then read number.txt, add 23, write the result to /app/submission/answer.txt "
            "and finish. Explain each action briefly. No subagents, web, or credentials inspection."
        ),
        Validation(private),
        Agent(
            "gpt-6-astra",
            harness="bounded-codex" if bounded else "codex",
            options={
                **({"max_turns": 20} if bounded else {}),
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
        Env(
            sandbox=Sandbox(
                setup=(
                    "apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*",
                    "npm install -g @openai/codex@0.162.1",
                ),
                internet="allowlist",
                allowed_hosts=("chatgpt.com", "auth.openai.com", "api.openai.com"),
            )
        ),
        Budget(seconds=300),
    )
    async with experiment.start(directory=root / "original", backend="modal") as run:
        for _ in range(100):
            result = await run.environment.exec(
                "test -f /app/ready", user="root", timeout_sec=10
            )
            if result.return_code == 0:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Codex did not reach the checkpoint marker")
        saved = await run.checkpoint("native-session")
        print("native session:", saved.metadata["native_session"], flush=True)
        if bounded:
            used = saved.metadata["actions_used"]
            if not 0 < used < 20:
                raise RuntimeError("checkpoint did not retain the live action budget")
            print("actions already used:", used, flush=True)
    result = await experiment.run(
        directory=root / "restored", backend="modal", resume_from=saved
    )
    print("restored:", root, result.rewards, flush=True)
    if result.rewards != [1.0]:
        raise RuntimeError("native Codex resume failed; inspect retained records")
    if bounded:
        records = result.directory / result.trials[0].records
        status = json.loads((records / "agent/budget.json").read_text())
        if (
            status["actions_used_before_resume"] != saved.metadata["actions_used"]
            or not saved.metadata["actions_used"] < status["turns"] <= 20
            or status["max_seconds"] > saved.metadata["remaining_seconds"]
            or not status["cleanup_complete"]
        ):
            raise RuntimeError(
                "restored supervisor did not preserve limits and cleanup"
            )
        print(
            "restored action budget:",
            status["turns"],
            "of",
            status["max_turns"],
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bounded",
        action="store_true",
        help="Check managed policy and action-budget recovery",
    )
    asyncio.run(main(bounded=parser.parse_args().bounded))
