"""Prove trusted host preparation reaches isolated validation without learner spoofing."""

import argparse
import asyncio
import json
from pathlib import Path

from sprint import Agent, Budget, Experiment, Task, Validation


async def main(records):
    records.mkdir(parents=True, mode=0o700, exist_ok=False)
    private = records / "validation"
    private.mkdir()
    (private / "secret.txt").write_text("operator-only-fixture")
    (private / "prepare.py").write_text(
        "async def prepare(context):\n"
        '    if (context.private / "secret.txt").read_text() != "operator-only-fixture":\n'
        '        raise RuntimeError("private preparation input missing")\n'
        '    context.record("prepared", {"status": "complete"})\n'
        '    return {"measurement": 0.75, "source": "operator"}\n'
    )
    (private / "evaluate.py").write_text(
        "import json\n"
        "def verify(context):\n"
        '    record = context.records / "sprint-resources/operator-validation.json"\n'
        "    value = json.loads(record.read_text())\n"
        '    if value.get("source") != "operator": raise RuntimeError("spoofed preparation")\n'
        '    return {"score": value["measurement"]}\n'
    )
    solve = records / "solve.sh"
    solve.write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/submission /logs/agent/sprint-resources\n"
        'printf \'{"measurement":1,"source":"learner"}\' > /logs/agent/sprint-resources/operator-validation.json\n'
        "printf ready > /app/submission/answer.txt\n"
    )
    experiment = Experiment(
        Task("Write a submission; operator preparation is private."),
        Validation(private, prepare="prepare.py", reference=solve),
        Agent("unused", harness="oracle"),
        budget=Budget(seconds=120),
    )
    result = await experiment.run(directory=records / "run", backend="modal")
    if result.rewards != [0.75]:
        raise RuntimeError(f"operator preparation failed; inspect {records}")
    print(
        json.dumps(
            {
                "passed": True,
                "reward": result.rewards,
                "learner_spoof_ignored": True,
                "directory": str(records),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    asyncio.run(main(parser.parse_args().records))
