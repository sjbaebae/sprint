"""Stable operator-facing results and a self-contained, escaped review page."""

import html
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class TrialResult:
    """One independent trial; an unscored failure is distinct from reward zero."""

    name: str
    status: str
    reward: float | None
    started_at: str | None
    finished_at: str | None
    records: str
    validation: dict[str, Any]
    error: dict[str, Any] | None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    """Backend-independent result, persisted alongside native provider records."""

    directory: Path
    trials: tuple[TrialResult, ...]

    @property
    def rewards(self):
        """Finalized rewards only; never impute zero for missing evaluation."""
        return [trial.reward for trial in self.trials if trial.reward is not None]

    def save(self):
        """Write machine-readable results and a vertical offline review page."""
        payload = {"schema_version": 1, "trials": [asdict(t) for t in self.trials]}
        interruption = self.directory / "interruption.json"
        if interruption.is_file():
            payload["interruption"] = json.loads(interruption.read_text())
        cleanup = self.directory / "cleanup-errors.json"
        if cleanup.is_file():
            payload["cleanup_errors"] = json.loads(cleanup.read_text())
        (self.directory / "result.json").write_text(
            json.dumps(payload, indent=2, allow_nan=False)
        )
        sections = ["<h1>Run review</h1>"]
        if "interruption" in payload:
            sections.append("<h2>Run interrupted</h2>" + pre(payload["interruption"]))
        cleanup = self.directory / "cleanup-errors.json"
        if cleanup.is_file():
            sections.append(
                "<h2>Unresolved resource cleanup failures</h2>"
                + pre(cleanup.read_text())
            )
        events = self.directory / "events.jsonl"
        if events.is_file():
            sections.append("<h2>Lifecycle times</h2>" + pre(events.read_text()))
        for trial in self.trials:
            sections.append(f"<h2>{html.escape(trial.name)}</h2>")
            sections.append(pre(asdict(trial)))
            resources = self.directory / "resources" / trial.name
            if resources.is_dir():
                sections.append("<h3>Worker execution records</h3>")
                for journal in sorted(resources.glob("*/jobs.jsonl")):
                    sections.append(
                        "<h4>"
                        + html.escape(journal.parent.name)
                        + "</h4>"
                        + pre(journal.read_text())
                    )
                    for source in sorted(journal.parent.glob("*.py")):
                        sections.append(
                            "<h4>Submitted code: "
                            + html.escape(source.name)
                            + "</h4>"
                            + pre(source.read_text())
                        )
                for journal in sorted(resources.glob("*.jsonl")):
                    sections.append(
                        "<h4>Resource service events</h4>" + pre(journal.read_text())
                    )
                for feedback in sorted(resources.glob("*.json")):
                    sections.append(
                        "<h4>Request feedback</h4>" + pre(feedback.read_text())
                    )
            records = self.directory / trial.records
            trajectory = records / "agent/trajectory.json"
            if trajectory.is_file() and not trajectory.is_symlink():
                data = read_trajectory(trajectory)
                if data.get("error"):
                    sections.append(
                        "<h3>Transcript unavailable</h3>" + pre(data["error"])
                    )
                turn = 0
                for step in data.get("steps", []):
                    if step.get("source") == "agent":
                        turn += 1
                        heading = f"Turn {turn}: agent"
                    else:
                        heading = (
                            f"Conversation context: {step.get('source', 'unknown')}"
                        )
                    sections.append(f"<h3>{html.escape(heading)}</h3>")
                    sections.append(
                        pre(step.get("timestamp") or "Timestamp unavailable")
                    )
                    sections.append("<h4>Message</h4>" + pre(step.get("message", "")))
                    if step.get("reasoning_content"):
                        sections.append(
                            "<h4>Recorded reasoning</h4>"
                            + pre(step["reasoning_content"])
                        )
                    elif step.get("source") == "agent":
                        sections.append(
                            "<p>No reasoning summary recorded for this turn.</p>"
                        )
                    for call in step.get("tool_calls") or []:
                        sections.append("<h4>Tool call</h4>" + pre(call))
                    if step.get("observation"):
                        sections.append(
                            "<h4>Tool result</h4>" + pre(step["observation"])
                        )
            else:
                sections.append("<p>No structured agent trajectory was recorded.</p>")
        (self.directory / "review.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Run review</title>'
            "<style>body{max-width:1000px;margin:32px auto;padding:0 24px;"
            "font:16px system-ui;line-height:1.5}pre{white-space:pre-wrap;"
            "overflow-wrap:anywhere;background:#f3f4f6;padding:16px}"
            "h2{border-top:2px solid #ddd;padding-top:24px}</style>"
            + "\n".join(sections)
        )
        return self


def read_trajectory(path):
    """A malformed transcript is a visibility failure, not a change to reward."""
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("steps", []), list):
            raise ValueError("trajectory must contain a steps list")
        if any(not isinstance(step, dict) for step in data.get("steps", [])):
            raise ValueError("trajectory steps must be objects")
        for step in data.get("steps", []):
            calls = step.get("tool_calls")
            if calls is not None and (
                not isinstance(calls, list)
                or any(not isinstance(call, dict) for call in calls)
            ):
                raise ValueError("trajectory tool_calls must be a list of objects")
        json.dumps(data, allow_nan=False)
        return data
    except (OSError, ValueError) as error:
        return {"steps": [], "error": f"{type(error).__name__}: {error}"}


def pre(value):
    """Escape all learner-controlled content; never render transcript HTML."""
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False)
    return "<pre>" + html.escape(value) + "</pre>"


def read_grade(path):
    """Quarantine malformed private grading without losing other trial records."""
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("grading result must be a regular file")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("grading result must be an object")
        json.dumps(value, allow_nan=False)
        status = value.get("status")
        if status not in {"graded", "disqualified", "quarantined"}:
            raise ValueError("unknown grading status")
        reward = value.get("reward")
        if status == "quarantined":
            if "reward" in value:
                raise ValueError("quarantined grading must not contain reward")
        elif (
            type(reward) not in (int, float)
            or not math.isfinite(reward)
            or not 0 <= reward <= 1
        ):
            raise ValueError("final reward must be finite and in [0, 1]")
        elif status == "disqualified" and reward != 0:
            raise ValueError("disqualified grading must have zero reward")
        from .validation import validate_violation_groups

        violations = validate_violation_groups(
            value.get("violations", {"confirmed": [], "suspected": []})
        )
        if status == "disqualified" and not violations["confirmed"]:
            raise ValueError("disqualification requires confirmed violation evidence")
        if status == "graded" and any(violations.values()):
            raise ValueError("graded result contains unresolved violations")
        return value
    except (OSError, ValueError, TypeError) as error:
        return {
            "status": "quarantined",
            "error_type": "MalformedGradingResult",
            "message": str(error),
        }


def inspect_artifact(directory, records, submission):
    """An inspection failure is retained without changing trusted grading."""
    try:
        return artifact_state(directory, records, submission)
    except (OSError, ValueError) as error:
        return {
            "scope": submission,
            "status": "inspection_error",
            "error_type": type(error).__name__,
            "message": str(error),
        }


def collect(
    directory, job_directory, native, *, submission="/app/submission", interruption=None
):
    """Translate a completed Harbor job without exposing its model classes."""
    directory, job_directory = Path(directory), Path(job_directory)
    trials = []
    for trial in native.trial_results:
        records = job_directory / trial.trial_name
        grade_path = records / "verifier/result.json"
        grade = read_grade(grade_path)
        error = (
            trial.exception_info.model_dump(mode="json")
            if trial.exception_info
            else None
        )
        reward = grade.get("reward")
        # Runtime errors invalidate reward; quarantine is not an agent failure.
        status = grade.get("status", "error" if error else "unscored")
        if (
            error
            and error.get("exception_type") != "AgentTimeoutError"
            and status != "quarantined"
        ):
            status, reward = "error", None
        if error and error.get("exception_type") == "CancelledError":
            status, reward = "cancelled", None
        if interruption is not None and trial.finished_at is None:
            status = (
                "cancelled"
                if interruption["exception_type"] == "CancelledError"
                else "error"
            )
            reward, error = None, interruption
        trials.append(
            TrialResult(
                name=trial.trial_name,
                status=status,
                reward=reward,
                started_at=trial.started_at.isoformat() if trial.started_at else None,
                finished_at=trial.finished_at.isoformat()
                if trial.finished_at
                else None,
                records=str(records.relative_to(directory)),
                validation=grade,
                error=error,
                state=inspect_artifact(directory, records, submission),
            )
        )
    return RunResult(directory, tuple(trials)).save()


def collect_interrupted(
    directory, job_directory, error, *, submission="/app/submission"
):
    """Preserve completed outcomes and partial logs without swallowing failure."""
    from sprint.models.trial.result import TrialResult as NativeTrialResult

    directory, job_directory = Path(directory), Path(job_directory)
    details = {
        "exception_type": type(error).__name__,
        "message": str(error),
        "recorded_at": datetime.now(UTC).isoformat(),
        "unreadable_records": [],
    }
    trials = []
    for path in sorted(job_directory.glob("*/result.json")):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("native result must be a regular operator record")
            trial = NativeTrialResult.model_validate_json(path.read_text())
            if trial.trial_name != path.parent.name:
                raise ValueError("native trial name differs from its record directory")
            trials.append(trial)
        except (OSError, ValueError) as failure:
            details["unreadable_records"].append(
                {"path": str(path), "message": str(failure)}
            )
    (directory / "interruption.json").write_text(json.dumps(details, indent=2))
    return collect(
        directory,
        job_directory,
        SimpleNamespace(trial_results=trials),
        submission=submission,
        interruption=details,
    )


def artifact_state(directory, records, submission):
    """Compare initial and final submitted files after the sandbox has stopped.

    Only the declared submission tree is covered, not the entire live workspace.
    Provider artifact transfer must already have produced a quiescent local export.
    """
    from .state import StateTracker
    from .validation import unsafe_artifact

    final = records / "artifacts" / submission.lstrip("/")
    scope = {"scope": submission}
    if not final.exists():
        return {**scope, "status": "not_collected"}
    unsafe = unsafe_artifact(final)
    if unsafe:
        return {**scope, "status": "unsafe", "reason": unsafe}
    initial = (
        directory / "task/environment/workspace" / submission.removeprefix("/app/")
    )
    if not initial.is_dir():
        initial = records / "state-empty"
        initial.mkdir(exist_ok=True)
    store = records / "state"
    if store.exists() or store.is_symlink():
        store = records / ("state-" + uuid4().hex)
    before = StateTracker(initial, store).capture("before")
    after = StateTracker(final, store).capture("after")
    changes = StateTracker.diff(before, after)
    (store / "diff.json").write_text(json.dumps(changes, indent=2))
    return {
        **scope,
        "status": "captured",
        "changes": changes,
        "records": str(store.relative_to(directory)),
    }
