"""Standalone verifier runtime, copied into the separate verifier image."""

import importlib.util
import json
import math
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Context:
    """Read-only-by-contract artifact paths; never run learner code in this process."""

    submission: Path
    private: Path
    records: Path | None = None


def component(value):
    """Require normalized scores and JSON-serializable metrics/evidence."""
    if not isinstance(value, dict):
        raise TypeError("evaluation must return an object")
    score = value.get("score")
    if (
        type(score) not in (float, int)
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        raise ValueError("score must be a finite number in [0, 1]")
    json.dumps(value, allow_nan=False)
    return value


def unsafe_artifact(root):
    """Reject links and special files in the final, quiescent artifact tree."""
    root = Path(root)
    if root.is_symlink():
        return "submission root is a symbolic link"
    if not root.exists():
        return None  # Missing artifact is an ordinary outcome for the verifier.
    if not root.is_dir():
        return "submission root must be a directory"
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                return f"submission contains a link or special file: {path.relative_to(root)}"
    return None


def evaluate(module, context, *, weights=(1.0, 0.0), rubric=None):
    """Disqualifications gate grading; suspicion or grader errors remain unscored.

    Deterministic checks must supply evidence for confirmed violations. Model
    allegations alone belong in suspected. Failures never receive a default score.
    """
    result: dict[str, Any] = {
        "status": "graded",
        "violations": {"confirmed": [], "suspected": []},
    }
    # A submitted link could point into private evaluation files; a FIFO could
    # hang the grader. Inspect the quiescent transfer before any author code runs.
    unsafe = unsafe_artifact(context.submission)
    if unsafe:
        result["violations"]["confirmed"].append(
            {
                "id": "unsafe_artifact",
                "evidence": [unsafe],
            }
        )
        return {**result, "status": "disqualified", "reward": 0.0}
    if hasattr(module, "disqualify"):
        violations = module.disqualify(context)
        if not isinstance(violations, dict) or set(violations) != {
            "confirmed",
            "suspected",
        }:
            raise ValueError("disqualify must return confirmed and suspected lists")
        for group in violations.values():
            if not isinstance(group, list) or any(
                not isinstance(v, dict) or not v.get("id") or not v.get("evidence")
                for v in group
            ):
                raise ValueError("each violation requires id and evidence")
        result["violations"] = violations
        if violations["confirmed"]:
            return {**result, "status": "disqualified", "reward": 0.0}
        if violations["suspected"]:
            return {**result, "status": "quarantined"}
    result["verifier"] = component(module.verify(context))
    if weights[1]:
        result["judge"] = component(module.judge(context, rubric))
    result["reward"] = weights[0] * result["verifier"]["score"] + weights[
        1
    ] * result.get("judge", {}).get("score", 0)
    return result


def main(config_path):
    """Emit a detailed result; write Harbor reward only for finalized judgments."""
    config = json.loads(Path(config_path).read_text())
    private = Path(config_path).parent / "bundle"
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    # Prevent stale reward acceptance if invoked repeatedly in the same verifier.
    (output / "reward.json").unlink(missing_ok=True)
    try:
        sys.path.insert(0, str(private))
        spec = importlib.util.spec_from_file_location(
            "world_validation", private / config["entrypoint"]
        )
        if spec is None or spec.loader is None:
            raise ValueError("validation entrypoint must be an importable Python file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        rubric = (private / config["rubric"]).read_text() if config["rubric"] else None
        result = evaluate(
            module,
            Context(Path(config["submission"]), private, Path("/logs/research-agent")),
            weights=config["weights"],
            rubric=rubric,
        )
    except Exception as exc:  # noqa: BLE001 -- quarantine arbitrary author/provider failures
        result = {
            "status": "quarantined",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    (output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    if "reward" not in result:
        raise SystemExit(2)
    (output / "reward.json").write_text(
        json.dumps({"reward": result["reward"]}, allow_nan=False)
    )


if __name__ == "__main__":
    main(sys.argv[1])
