import json
import math
import tomllib
from types import SimpleNamespace

import pytest

from sprint import Agent, Experiment, Sandbox, StateTracker, Task, Validation, Env
from sprint.validation import Context, combine_components, evaluate


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
        Env(files=public),
    )


def test_compile_keeps_private_files_out_of_learner(tmp_path):
    pytest.importorskip("sprint")
    from sprint.models.task.task import Task as HarborTask

    experiment = make_experiment(tmp_path)
    task = experiment.compile(tmp_path / "task")
    assert (task / "environment/workspace/starter.py").exists()
    assert not list((task / "environment").rglob("secret.txt"))
    assert (task / "tests/bundle/secret.txt").exists()
    parsed = tomllib.loads((task / "task.toml").read_text())
    assert parsed["verifier"]["environment_mode"] == "separate"
    assert parsed["agent"]["network_mode"] == "no-network"
    assert parsed["environment"]["network_mode"] == "no-network"
    assert parsed["verifier"]["environment"]["gpus"] == 0
    assert parsed["verifier"]["environment"]["network_mode"] == "no-network"
    assert parsed["verifier"]["network_mode"] == "no-network"
    HarborTask(task)
    with pytest.raises(FileExistsError):
        experiment.compile(task)


def test_bounded_harness_policy_is_operator_owned_and_matches_network(tmp_path):
    from dataclasses import replace
    from sprint import Worker

    experiment = make_experiment(tmp_path)
    for network in ("none", "full"):
        sandbox = Sandbox(internet=network)
        configured = replace(
            experiment,
            agent=Agent("test", harness="bounded-codex"),
            env=replace(
                experiment.env,
                sandbox=sandbox,
                attachments={
                    "train": Worker(shell=True, backend="modal", sandbox=sandbox),
                },
            ),
        )
        task = configured.compile(tmp_path / network)
        learner = task / "environment"
        policy = tomllib.loads((learner / "codex-requirements.toml").read_text())
        assert policy["permissions"]["offline"]["network"]["enabled"] == (
            network == "full"
        )
        assert policy["features"]["multi_agent"] is False
        assert policy["features"]["apps"] is False
        assert policy["allowed_approval_policies"] == ["never"]
        assert policy["allowed_permission_profiles"] == {"offline": True}
        dockerfile = (learner / "Dockerfile").read_text()
        assert dockerfile.index(
            "chmod 444 /etc/codex/requirements.toml"
        ) < dockerfile.index("USER agent")
        operations = json.loads((learner / "resource_manifest.json").read_text())[
            "train"
        ]["operations"]
        assert ("connect" in operations) == (network == "full")
        assert not list((task / "tests").glob("codex-requirements.toml"))


def test_compile_rejects_overlapping_public_private(tmp_path):
    pytest.importorskip("sprint")
    experiment = make_experiment(tmp_path)
    experiment = Experiment(
        experiment.task, experiment.validation, experiment.agent, Env(files=tmp_path)
    )
    with pytest.raises(ValueError, match="separate trees"):
        experiment.compile(tmp_path / "task")


def test_compile_rejects_symlink_bundle(tmp_path):
    pytest.importorskip("sprint")
    experiment = make_experiment(tmp_path)
    (experiment.env.files / "leak").symlink_to(experiment.validation.bundle)
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
    combined = combine_components(
        result["verifier"], result["judge"], weights=(0.8, 0.2)
    )
    assert combined["reward"] == result["reward"]
    with pytest.raises(TypeError):
        combine_components({"score": 0.5}, weights=(0.8, 0.2))


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


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "fifo", "root_link"])
def test_unsafe_artifact_cannot_read_private_files_or_hang_grader(tmp_path, kind):
    import os

    submission = tmp_path / "submission"
    private = tmp_path / "private"
    private.mkdir()
    (private / "answer").write_text("secret")
    if kind == "root_link":
        submission.symlink_to(private, target_is_directory=True)
    else:
        submission.mkdir()
        if kind == "fifo":
            os.mkfifo(submission / "answer")
        else:
            target = private if kind == "directory_link" else private / "answer"
            (submission / "answer").symlink_to(target)

    def never(context):
        raise AssertionError("unsafe artifact must not reach evaluation")

    result = evaluate(SimpleNamespace(verify=never), Context(submission, private))
    assert result["status"] == "disqualified"
    assert result["reward"] == 0
    assert result["violations"]["confirmed"][0]["id"] == "unsafe_artifact"


def test_gpu_and_validation_resources_are_independent(tmp_path):
    from sprint import Resources

    experiment = make_experiment(tmp_path)
    experiment = Experiment(
        experiment.task,
        experiment.validation,
        experiment.agent,
        Env(files=experiment.env.files, resources=Resources(gpu="L4", memory_mb=16384)),
    )
    task = experiment.compile(tmp_path / "task")
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["environment"]["gpu_types"] == ["L4"]
    assert config["environment"]["gpus"] == 1
    assert config["environment"]["memory_mb"] == 16384
    assert config["verifier"]["environment"]["gpus"] == 0
    assert config["verifier"]["environment"]["memory_mb"] == 4096


def test_agent_trace_requires_separate_verifier():
    from sprint.models.task.config import VerifierConfig

    with pytest.raises(ValueError, match="separate"):
        VerifierConfig(include_agent_trace=True, environment_mode="shared")


def test_named_files_are_separate_read_only_attachments(tmp_path):
    from dataclasses import replace
    from sprint import Files

    experiment = make_experiment(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "rows.txt").write_text("public rows")
    experiment = replace(
        experiment, env=replace(experiment.env, attachments={"data": Files(data)})
    )
    task = experiment.compile(tmp_path / "task")
    assert (task / "environment/resources/data/rows.txt").read_text() == "public rows"
    assert not (task / "tests/bundle/rows.txt").exists()
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert "COPY resources/data/ /resources/data/" in dockerfile
    assert "chown -R root:root /resources" in dockerfile
    assert "chmod 444" in dockerfile
    assert "/resources/data" in (task / "instruction.md").read_text()


@pytest.mark.parametrize("name", ["../data", "a/b", "data;id", "", "with space"])
def test_bad_attachment_names_are_rejected(tmp_path, name):
    from sprint import Files

    with pytest.raises(ValueError, match="attachment names"):
        Env(attachments={name: Files(tmp_path)})


def test_private_attachment_and_link_are_rejected(tmp_path):
    from dataclasses import replace
    from sprint import Files

    experiment = make_experiment(tmp_path)
    private_world = replace(
        experiment.env, attachments={"data": Files(experiment.validation.bundle)}
    )
    with pytest.raises(ValueError, match="separate trees"):
        replace(experiment, env=private_world).compile(tmp_path / "bad")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "secret").symlink_to(experiment.validation.bundle / "secret.txt")
    with pytest.raises(ValueError, match="symlink"):
        replace(
            experiment,
            env=replace(experiment.env, attachments={"data": Files(linked)}),
        ).compile(tmp_path / "linked-task")


def test_worker_and_agent_share_public_data_but_not_private_validation(tmp_path):
    from dataclasses import replace
    from sprint import Files, Worker

    experiment = make_experiment(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "rows.txt").write_text("public")
    experiment = replace(
        experiment,
        env=replace(
            experiment.env, attachments={"data": Files(data), "train": Worker()}
        ),
    )
    task = experiment.compile(tmp_path / "task")
    assert (task / "environment/resources/data/rows.txt").read_text() == "public"
    assert (task / "workers/train/resources/data/rows.txt").read_text() == "public"
    assert (task / "workers/train/worker_export.py").exists()
    assert not list((task / "workers").rglob("secret.txt"))
    assert "chmod 444" in (task / "workers/train/Dockerfile").read_text()


def test_operator_setup_is_built_before_dropping_privileges():
    from sprint.backends.runtime import dockerfile

    image = dockerfile(Sandbox(setup=("install-toolchain",)), copy_workspace=False)
    assert image.index("RUN install-toolchain") < image.index("USER agent")
    assert image.index("RUN install-toolchain") < image.index("RUN useradd")


def test_api_auth_requires_host_key_and_overrides_subscription(monkeypatch):
    from sprint.backends.runtime import agent_auth_env

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        agent_auth_env(Agent("test", auth="api-key"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    resolved = agent_auth_env(Agent("test", auth="api-key"))
    assert resolved["OPENAI_API_KEY"] == "test-key-not-real"
    assert resolved["CODEX_AUTH_JSON_PATH"] == ""
    assert resolved["CODEX_FORCE_AUTH_JSON"] == "0"


def test_claude_api_auth_suppresses_oauth_and_serialization_redacts(monkeypatch):
    from sprint.backends.runtime import agent_auth_env
    from sprint.models.trial.config import AgentConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    agent = Agent("test", harness="claude-code", auth="api-key")
    env = agent_auth_env(agent)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["CLAUDE_FORCE_OAUTH"] == "0"
    config = AgentConfig(name="claude-code", model_name="test", env=env)
    assert "test-key-not-real" not in config.model_dump_json()
    assert agent_auth_env(Agent("test")) == {}


def test_default_harness_bootstrap_preserves_custom_images(tmp_path):
    from dataclasses import replace

    base = make_experiment(tmp_path)
    experiment = replace(
        base, agent=Agent("test", harness="claude-code", options={"version": "2.0.0"})
    )
    compiled = experiment.compile(tmp_path / "claude")
    assert (
        "npm install -g @anthropic-ai/claude-code@2.0.0"
        in (compiled / "environment/Dockerfile").read_text()
    )
    assert "npm install" not in (compiled / "tests/Dockerfile").read_text()
    managed = json.loads(
        (compiled / "environment/claude-managed-settings.json").read_text()
    )
    assert {"Agent", "Task", "TeamCreate", "WebSearch", "WebFetch"} <= set(
        managed["permissions"]["deny"]
    )
    assert (
        "/etc/claude-code/managed-settings.json"
        in (compiled / "environment/Dockerfile").read_text()
    )
    assert not list((compiled / "tests").rglob("claude-managed-settings.json"))
    custom = replace(
        experiment,
        env=replace(
            experiment.env,
            sandbox=Sandbox(image="custom/tools:1", setup=("echo task-tools",)),
        ),
    )
    compiled_custom = custom.compile(tmp_path / "custom")
    docker = (compiled_custom / "environment/Dockerfile").read_text()
    assert "FROM custom/tools:1" in docker and "RUN echo task-tools" in docker
    assert "apt-get" not in docker and "npm install" not in docker
