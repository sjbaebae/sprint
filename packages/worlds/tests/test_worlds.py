import json
import math
import tomllib
from types import SimpleNamespace

import pytest

from worlds import Agent, Experiment, Sandbox, StateTracker, Task, Validation, World
from worlds.validation import Context, evaluate


def make_experiment(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "evaluate.py").write_text("def verify(context): return {'score': 1.0}\n")
    (private / "secret.txt").write_text("private expected answer")
    public = tmp_path / "public"
    public.mkdir()
    (public / "starter.py").write_text("print('hello')\n")
    return Experiment(
        Task("Solve the task"),
        Validation(private),
        Agent("test-model"),
        World(files=public),
    )


def test_compile_keeps_private_files_out_of_learner(tmp_path):
    pytest.importorskip("harbor")
    from harbor.models.task.task import Task as HarborTask

    experiment = make_experiment(tmp_path)
    task = experiment.compile(tmp_path / "task")
    assert (task / "environment/workspace/starter.py").exists()
    assert not list((task / "environment").rglob("secret.txt"))
    assert (task / "tests/bundle/secret.txt").exists()
    parsed = tomllib.loads((task / "task.toml").read_text())
    assert parsed["verifier"]["environment_mode"] == "separate"
    assert parsed["agent"]["network_mode"] == "no-network"
    assert parsed["verifier"]["network_mode"] == "no-network"
    HarborTask(task)
    with pytest.raises(FileExistsError):
        experiment.compile(task)


def test_compile_rejects_overlapping_public_private(tmp_path):
    pytest.importorskip("harbor")
    experiment = make_experiment(tmp_path)
    experiment = Experiment(
        experiment.task, experiment.validation, experiment.agent, World(files=tmp_path)
    )
    with pytest.raises(ValueError, match="separate trees"):
        experiment.compile(tmp_path / "task")


def test_compile_rejects_symlink_bundle(tmp_path):
    pytest.importorskip("harbor")
    experiment = make_experiment(tmp_path)
    (experiment.world.files / "leak").symlink_to(experiment.validation.bundle)
    with pytest.raises(ValueError, match="symlink"):
        experiment.compile(tmp_path / "task")


@pytest.mark.parametrize(
    "internet,hosts", [("none", ("example.com",)), ("allowlist", ()), ("typo", ())]
)
def test_network_policy_is_explicit(internet, hosts):
    with pytest.raises(ValueError):
        Sandbox(internet=internet, allowed_hosts=hosts)


@pytest.mark.parametrize(
    "path", ["/", "/app", "/etc/secret", "/app/../etc", "relative"]
)
def test_submission_path_cannot_escape_workspace(path):
    with pytest.raises(ValueError):
        Task("task", submission=path)


def test_verifier_and_judge_weighting(tmp_path):
    module = SimpleNamespace(
        verify=lambda c: {"score": 0.5},
        judge=lambda c, r: {"score": 1, "evidence": [r]},
    )
    result = evaluate(
        module, Context(tmp_path, tmp_path), weights=(0.8, 0.2), rubric="rubric"
    )
    assert result["reward"] == pytest.approx(0.6)
    assert result["judge"]["evidence"] == ["rubric"]


@pytest.mark.parametrize("score", [math.nan, math.inf, -1, 2, True, "0.5", None])
def test_bad_grade_never_becomes_reward(tmp_path, score):
    with pytest.raises(ValueError):
        evaluate(
            SimpleNamespace(verify=lambda c: {"score": score}),
            Context(tmp_path, tmp_path),
        )


@pytest.mark.parametrize(
    "category,status,reward",
    [("confirmed", "disqualified", 0), ("suspected", "quarantined", None)],
)
def test_violation_handling_does_not_execute_grader(tmp_path, category, status, reward):
    def never(context):
        raise AssertionError("grader should not run")

    violations = {"confirmed": [], "suspected": []}
    violations[category] = [{"id": "protected_data", "evidence": ["trusted record"]}]
    module = SimpleNamespace(disqualify=lambda c: violations, verify=never)
    result = evaluate(module, Context(tmp_path, tmp_path))
    assert result["status"] == status
    assert result.get("reward") == reward


def test_grader_exception_is_not_a_zero_score(tmp_path):
    def broken(context):
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError):
        evaluate(SimpleNamespace(verify=broken), Context(tmp_path, tmp_path))


def test_snapshots_capture_changes_and_do_not_follow_links(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("do not read")
    (work / "link").symlink_to(outside)
    (work / "edit.py").write_text("a = 1\n")
    (work / "delete.txt").write_text("old\n")
    tracker = StateTracker(work, tmp_path / "records")
    before = tracker.capture("before", turn=4)
    (work / "edit.py").write_text("a = 2\n")
    (work / "delete.txt").unlink()
    (work / "add.txt").write_text("new\n")
    after = tracker.capture("after", turn=4)
    changes = {c["path"]: c for c in tracker.diff(before, after)}
    assert changes["edit.py"]["status"] == "modified"
    assert "+a = 2" in changes["edit.py"]["patch"]
    assert changes["delete.txt"]["status"] == "deleted"
    assert changes["add.txt"]["status"] == "added"
    assert before["files"]["link"]["kind"] == "symlink"
    assert "do not read" not in json.dumps(before)
    assert before["turn"] == 4 and before["timestamp"]
    with pytest.raises(FileExistsError):
        tracker.capture("before")


def test_oversized_content_is_explicitly_unverified(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "large").write_text("0123456789")
    tracker = StateTracker(work, tmp_path / "records", max_file_bytes=5)
    before = tracker.capture("a")
    after = tracker.capture("b")
    assert tracker.diff(before, after)[0]["status"] == "unverified"
    with pytest.raises(ValueError):
        StateTracker(work, work / "snapshots")
