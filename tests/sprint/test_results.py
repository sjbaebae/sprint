"""Ensure failures and learner-controlled traces remain honest in review records."""

import json
from types import SimpleNamespace

import pytest

from sprint.results import RunResult, TrialResult, collect


def test_review_escapes_trace_and_preserves_calls(tmp_path):
    agent = tmp_path / "native/trial/agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "timestamp": None,
                        "message": "<script>alert('bad')</script>",
                        "reasoning_content": "Check baseline first",
                        "tool_calls": [
                            {
                                "function_name": "bash",
                                "arguments": {"command": "sleep 60"},
                            }
                        ],
                        "observation": {"output": "completed"},
                    }
                ]
            }
        )
    )
    run = RunResult(
        tmp_path,
        (
            TrialResult(
                "trial",
                "graded",
                0.0,
                None,
                None,
                "native/trial",
                {},
                None,
            ),
        ),
    ).save()
    page = (tmp_path / "review.html").read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "sleep 60" in page and "completed" in page
    assert "Timestamp unavailable" in page
    assert "Turn 1: agent" in page
    assert run.rewards == [0.0]


def test_quarantine_keeps_no_reward_despite_backend_exception(tmp_path):
    records = tmp_path / "native/trial/verifier"
    records.mkdir(parents=True)
    (records / "result.json").write_text(
        json.dumps(
            {
                "status": "quarantined",
                "message": "judge unavailable",
            }
        )
    )
    error = SimpleNamespace(model_dump=lambda **kw: {"exception_type": "VerifierError"})
    trial = SimpleNamespace(
        trial_name="trial", exception_info=error, started_at=None, finished_at=None
    )
    result = collect(
        tmp_path, tmp_path / "native", SimpleNamespace(trial_results=[trial])
    )
    assert result.trials[0].status == "quarantined"
    assert result.rewards == []
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["trials"][0]["reward"] is None


def test_runtime_failure_invalidates_existing_reward(tmp_path):
    records = tmp_path / "native/trial/verifier"
    records.mkdir(parents=True)
    (records / "result.json").write_text('{"status":"graded","reward":1}')
    error = SimpleNamespace(model_dump=lambda **kw: {"exception_type": "RuntimeError"})
    trial = SimpleNamespace(
        trial_name="trial", exception_info=error, started_at=None, finished_at=None
    )
    result = collect(
        tmp_path, tmp_path / "native", SimpleNamespace(trial_results=[trial])
    )
    assert result.trials[0].status == "error"
    assert result.rewards == []


def test_final_artifact_diff_is_scoped_to_submission(tmp_path):
    from sprint.results import artifact_state

    initial = tmp_path / "task/environment/workspace/submission"
    initial.mkdir(parents=True)
    (initial / "code.py").write_text("x = 1\n")
    final = tmp_path / "native/trial/artifacts/app/submission"
    final.mkdir(parents=True)
    (final / "code.py").write_text("x = 2\n")
    result = artifact_state(tmp_path, tmp_path / "native/trial", "/app/submission")
    assert result["scope"] == "/app/submission"
    assert result["changes"][0]["status"] == "modified"
    assert "+x = 2" in result["changes"][0]["patch"]
    assert (tmp_path / "native/trial/state/before/snapshot.json").is_file()
    assert (tmp_path / "native/trial/state/after/snapshot.json").is_file()


def test_agent_time_limit_still_grades_final_artifact(tmp_path):
    records = tmp_path / "native/trial/verifier"
    records.mkdir(parents=True)
    (records / "result.json").write_text('{"status":"graded","reward":0.75}')
    error = SimpleNamespace(
        model_dump=lambda **kw: {"exception_type": "AgentTimeoutError"}
    )
    trial = SimpleNamespace(
        trial_name="trial", exception_info=error, started_at=None, finished_at=None
    )
    result = collect(
        tmp_path, tmp_path / "native", SimpleNamespace(trial_results=[trial])
    )
    assert result.rewards == [0.75]
    assert result.trials[0].error["exception_type"] == "AgentTimeoutError"


def test_review_keeps_grade_with_malformed_transcript_and_shows_worker_records(
    tmp_path,
):
    agent = tmp_path / "native/trial/agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text("{broken")
    worker = tmp_path / "resources/trial/train"
    worker.mkdir(parents=True)
    (worker / "jobs.jsonl").write_text('{"event":"job-end","return_code":1}')
    (worker / "abc.py").write_text('print("<script>")')
    (worker.parent / "delivery.jsonl").write_text(
        '{"error_type":"OSError","message":"delivery retry"}'
    )
    (tmp_path / "events.jsonl").write_text(
        '{"event":"agent-start","timestamp":"observed"}'
    )
    result = RunResult(
        tmp_path,
        (TrialResult("trial", "graded", 0.5, None, None, "native/trial", {}, None),),
    ).save()
    page = (tmp_path / "review.html").read_text()
    assert result.rewards == [0.5]
    assert "Transcript unavailable" in page
    assert "job-end" in page
    assert "observed" in page
    assert "Resource service events" in page and "delivery retry" in page
    assert "<script>" not in page


def test_review_numbers_only_agent_turns_and_marks_missing_reasoning(tmp_path):
    agent = tmp_path / "native/trial/agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"step_id": 1, "source": "system", "message": "context"},
                    {"step_id": 2, "source": "user", "message": "task"},
                    {"step_id": 3, "source": "agent", "message": "action"},
                ]
            }
        )
    )
    RunResult(
        tmp_path,
        (TrialResult("trial", "graded", 1, None, None, "native/trial", {}, None),),
    ).save()
    page = (tmp_path / "review.html").read_text()
    assert "Turn 1: agent" in page
    assert "Turn 3: agent" not in page
    assert "No reasoning summary recorded" in page


@pytest.mark.parametrize(
    "grade",
    [
        "[]",
        "{broken",
        '{"status":"graded","reward":true}',
        '{"status":"graded","reward":NaN}',
        '{"status":"graded","reward":1.1}',
        '{"status":"quarantined","reward":1}',
        '{"status":"disqualified","reward":0.5}',
        '{"status":"unknown","reward":1}',
        '{"status":"graded"}',
        '{"status":"disqualified","reward":0}',
        '{"status":"graded","reward":1,"violations":{"confirmed":[],"suspected":[{"id":"suspect","evidence":["T:1"]}]}}',
    ],
)
def test_malformed_grading_quarantines_without_losing_campaign(tmp_path, grade):
    native = tmp_path / "native"
    for name, value in (("bad", grade), ("good", '{"status":"graded","reward":0.75}')):
        records = native / name / "verifier"
        records.mkdir(parents=True)
        (records / "result.json").write_text(value)
    trials = [
        SimpleNamespace(
            trial_name=name, exception_info=None, started_at=None, finished_at=None
        )
        for name in ("bad", "good")
    ]
    result = collect(tmp_path, native, SimpleNamespace(trial_results=trials))
    assert result.trials[0].status == "quarantined"
    assert result.trials[0].validation["error_type"] == "MalformedGradingResult"
    assert result.rewards == [0.75]
    json.loads((tmp_path / "result.json").read_text())


def test_artifact_inspection_failure_does_not_discard_grade(tmp_path, monkeypatch):
    import sprint.results as results

    records = tmp_path / "native/trial/verifier"
    records.mkdir(parents=True)
    (records / "result.json").write_text('{"status":"graded","reward":0.75}')

    def failed_inspection(*args):
        raise OSError("export unavailable")

    monkeypatch.setattr(results, "artifact_state", failed_inspection)
    trial = SimpleNamespace(
        trial_name="trial", exception_info=None, started_at=None, finished_at=None
    )
    result = collect(
        tmp_path, tmp_path / "native", SimpleNamespace(trial_results=[trial])
    )
    assert result.rewards == [0.75]
    assert result.trials[0].state["status"] == "inspection_error"


def test_review_reports_invalid_tool_calls_without_losing_grade(tmp_path):
    agent = tmp_path / "native/trial/agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        '{"steps":[{"source":"agent","tool_calls":7}]}'
    )
    result = RunResult(
        tmp_path,
        (TrialResult("trial", "graded", 0.5, None, None, "native/trial", {}, None),),
    ).save()
    assert result.rewards == [0.5]
    assert "Transcript unavailable" in (tmp_path / "review.html").read_text()


def test_artifact_inspection_can_repeat_and_observe_new_export(tmp_path):
    from sprint.results import artifact_state

    final = tmp_path / "native/trial/artifacts/app/submission"
    final.mkdir(parents=True)
    (final / "answer").write_text("first")
    first = artifact_state(tmp_path, tmp_path / "native/trial", "/app/submission")
    (final / "answer").write_text("second")
    second = artifact_state(tmp_path, tmp_path / "native/trial", "/app/submission")
    assert first["status"] == second["status"] == "captured"
    assert first["records"] != second["records"]
    assert "+second" in second["changes"][0]["patch"]


def test_disqualification_requires_evidence_and_retains_zero_reward(tmp_path):
    from sprint.results import read_grade

    path = tmp_path / "grade.json"
    path.write_text(
        json.dumps(
            {
                "status": "disqualified",
                "reward": 0,
                "violations": {
                    "confirmed": [
                        {
                            "id": "unsafe_artifact",
                            "evidence": ["submission contains a symlink"],
                        }
                    ],
                    "suspected": [],
                },
            }
        )
    )
    assert read_grade(path)["status"] == "disqualified"
    assert read_grade(path)["reward"] == 0
