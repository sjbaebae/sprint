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


OUTPUT = Path("/logs/verifier")


@dataclass(frozen=True)
class Context:
    """Read-only-by-contract artifact paths; never run learner code in this process."""

    submission: Path
    private: Path
    records: Path | None = None

    @staticmethod
    def grade_axes(criteria, evidence, *, expected_ids, maximum=3):
        """Grade explained rubric scores using a trusted evidence reference index."""
        return grade_axes(
            criteria, evidence, expected_ids=expected_ids, maximum=maximum
        )


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


def validate_violation_groups(value):
    """Require attributable confirmed/suspected records; do not infer violations."""
    if not isinstance(value, dict) or set(value) != {"confirmed", "suspected"}:
        raise ValueError("violations must contain confirmed and suspected lists")
    seen = set()
    for group in value.values():
        if not isinstance(group, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("evidence"), list)
            or any(
                not (
                    (isinstance(record, str) and record.strip())
                    or (isinstance(record, dict) and record)
                )
                for record in item["evidence"]
            )
            or not item["evidence"]
            for item in group
        ):
            raise ValueError(
                "each violation requires an ID and a nonempty evidence list of references or records"
            )
        for item in group:
            if item["id"] in seen:
                raise ValueError(
                    "violation IDs must be unique across confirmed and suspected"
                )
            seen.add(item["id"])
    json.dumps(value, allow_nan=False)
    return value


def validate_weights(weights):
    """Reject invalid scoring configuration before invoking any verifier."""
    if (
        len(weights) != 2
        or any(
            type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0
            for weight in weights
        )
        or not math.isclose(sum(weights), 1)
    ):
        raise ValueError("grading weights must be finite, nonnegative and sum to one")


def combine_components(verifier, judge=None, *, weights=(1.0, 0.0)):
    """Compose measured verifier/judge results using the same rules as evaluation.

    Useful when remote evaluation and judging have already completed. Callers
    must resolve integrity dispositions first; this function only combines valid
    normalized components and never manufactures a missing judge score.
    """
    validate_weights(weights)
    result: dict[str, Any] = {"verifier": component(verifier)}
    if weights[1]:
        result["judge"] = component(judge)
    result["reward"] = weights[0] * result["verifier"]["score"] + weights[
        1
    ] * result.get("judge", {}).get("score", 0)
    return result


def evaluate(module, context, *, weights=(1.0, 0.0), rubric=None):
    """Disqualifications gate grading; suspicion or grader errors remain unscored.

    Deterministic checks must supply evidence for confirmed violations. Model
    allegations alone belong in suspected. Failures never receive a default score.
    """
    validate_weights(weights)
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
        violations = validate_violation_groups(module.disqualify(context))
        result["violations"] = violations
        if violations["confirmed"]:
            return {**result, "status": "disqualified", "reward": 0.0}
        if violations["suspected"]:
            return {**result, "status": "quarantined"}
    verifier = component(module.verify(context))
    judge = module.judge(context, rubric) if weights[1] else None
    return {**result, **combine_components(verifier, judge, weights=weights)}


def main(config_path):
    """Emit a detailed result; write Harbor reward only for finalized judgments."""
    output = OUTPUT
    output.mkdir(parents=True, exist_ok=True)
    # Clear old outputs before parsing config or importing author code.
    for name in ("reward.json", "result.json"):
        (output / name).unlink(missing_ok=True)
    try:
        config = json.loads(Path(config_path).read_text())
        private = Path(config_path).parent / "bundle"
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


def grade_axes(criteria, evidence, *, expected_ids, maximum=3):
    """Validate equally weighted rubric judgments against a trusted evidence index.

    Each criterion supplies id, score, explanation and evidence reference IDs.
    Authors build the index from records supplied to the judge, not judge output.
    Every score requires a concrete explanation; missing references fail grading.
    This checks provenance and shape, not whether the judge's interpretation is true.
    """
    if type(maximum) is not int or maximum < 1:
        raise ValueError("maximum must be a positive integer")
    if (
        not isinstance(expected_ids, (list, tuple))
        or not expected_ids
        or any(
            not isinstance(identifier, str) or not identifier
            for identifier in expected_ids
        )
        or len(set(expected_ids)) != len(expected_ids)
    ):
        raise ValueError("expected_ids must name every rubric criterion exactly once")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("criteria must be a nonempty list")
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a trusted reference index")
    seen = set()
    for item in criteria:
        if not isinstance(item, dict):
            raise ValueError("criterion must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("criterion IDs must be unique nonempty strings")
        seen.add(identifier)
        score = item.get("score")
        if type(score) is not int or not 0 <= score <= maximum:
            raise ValueError("criterion score is outside the rubric scale")
        reason = item.get("explanation")
        refs = item.get("evidence")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("every criterion requires a concrete explanation")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in evidence for ref in refs)
        ):
            raise ValueError("every criterion requires resolvable evidence references")
    if seen != set(expected_ids):
        raise ValueError("judgments must cover exactly the expected rubric criteria")
    return component(
        {
            "score": sum(item["score"] for item in criteria)
            / (maximum * len(criteria)),
            "criteria": criteria,
        }
    )


if __name__ == "__main__":
    main(sys.argv[1])
