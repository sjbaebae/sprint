"""The authoring scaffold compiles privately without provisioning or credentials."""

import pytest
import typer
from types import SimpleNamespace

from sprint.cli.env import create_environment, load_experiment
from sprint.cli import env as cli
from sprint.spec import Agent


def test_scaffold_compiles_private_evaluation_and_preserves_existing_work(tmp_path):
    directory = create_environment(tmp_path / "example")
    source = directory / "environment.py"
    experiment = load_experiment(directory)
    compiled = experiment.compile(tmp_path / "compiled")
    assert (compiled / "tests/bundle/evaluate.py").read_text() == (
        directory / "validation/evaluate.py"
    ).read_text()
    assert not list((compiled / "environment/workspace").iterdir())
    assert (compiled / "environment/codex-requirements.toml").is_file()
    assert experiment.validation.weights == (1.0, 0.0)
    original = source.read_bytes()
    with pytest.raises(FileExistsError):
        create_environment(directory)
    assert source.read_bytes() == original


@pytest.mark.parametrize("status", ["graded", "quarantined"])
def test_authoring_runner_distinguishes_zero_reward_from_unscored_failure(
    tmp_path, monkeypatch, status
):
    async def run(**kwargs):
        return SimpleNamespace(
            directory=tmp_path,
            rewards=[0.0] if status == "graded" else [],
            trials=[SimpleNamespace(name="trial", status=status)],
        )

    monkeypatch.setattr(
        cli,
        "load_experiment",
        lambda directory: SimpleNamespace(agent=Agent("test", harness="nop"), run=run),
    )
    if status == "graded":
        cli.run_environment(tmp_path)
    else:
        with pytest.raises(typer.Exit) as error:
            cli.run_environment(tmp_path)
        assert error.value.exit_code == 1
