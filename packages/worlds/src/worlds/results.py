"""Stable operator-facing results and a self-contained, escaped review page."""

import html
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
        (self.directory / "result.json").write_text(
            json.dumps(payload, indent=2, allow_nan=False)
        )
        sections = ["<h1>Run review</h1>"]
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
                for step in data.get("steps", []):
                    heading = f"Turn {step.get('step_id', '?')}: {step.get('source', 'unknown')}"
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
        return data
    except (OSError, ValueError) as error:
        return {"steps": [], "error": f"{type(error).__name__}: {error}"}


def pre(value):
    """Escape all learner-controlled content; never render transcript HTML."""
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False)
    return "<pre>" + html.escape(value) + "</pre>"


def collect(directory, job_directory, native, *, submission="/app/submission"):
    """Translate a completed Harbor job without exposing its model classes."""
    directory, job_directory = Path(directory), Path(job_directory)
    trials = []
    for trial in native.trial_results:
        records = job_directory / trial.trial_name
        grade_path = records / "verifier/result.json"
        grade = json.loads(grade_path.read_text()) if grade_path.is_file() else {}
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
                state=artifact_state(directory, records, submission),
            )
        )
    return RunResult(directory, tuple(trials)).save()


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
    before = StateTracker(initial, store).capture("before")
    after = StateTracker(final, store).capture("after")
    changes = StateTracker.diff(before, after)
    (store / "diff.json").write_text(json.dumps(changes, indent=2))
    return {**scope, "status": "captured", "changes": changes}
