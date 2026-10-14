"""Create small environments using Sprint's public Python API."""

import asyncio
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from sprint.spec import Experiment
from sprint.utils.env import parse_bool_env_value

env_app = typer.Typer(no_args_is_help=True)


def create_environment(directory: Path):
    """Create a fresh authoring directory; never merge into existing work."""
    directory.mkdir(parents=True, exist_ok=False)
    template = Path(__file__).with_name("template-env")
    shutil.copyfile(template / "environment.py", directory / "environment.py")
    shutil.copytree(template / "validation", directory / "validation")
    (directory / ".gitignore").write_text(".sprint/\n__pycache__/\n")
    return directory


def load_experiment(directory: Path):
    """Import the operator-authored definition; this does not launch resources."""
    source = directory.resolve() / "environment.py"
    spec = importlib.util.spec_from_file_location("sprint_env_" + uuid4().hex, source)
    if spec is None or spec.loader is None:
        raise ValueError("environment.py must be an importable Python file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    original_path = list(sys.path)
    sys.path.insert(0, str(source.parent))
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    finally:
        sys.path[:] = original_path
    experiment = getattr(module, "experiment", None)
    if not isinstance(experiment, Experiment):
        raise ValueError("environment.py must define experiment = Experiment(...)")
    return experiment


@env_app.command("init")
def init_environment(
    directory: Annotated[Path, typer.Argument(help="New environment directory.")],
):
    """Create environment.py and a private evaluator. Starts no resources."""
    try:
        create_environment(directory)
    except FileExistsError as error:
        raise typer.BadParameter(
            "Choose a new directory; existing work is not overwritten."
        ) from error
    typer.echo(
        f"Created {directory / 'environment.py'} and {directory / 'validation/evaluate.py'}"
    )
    typer.echo(f"Inspect: uv run sprint env compile {directory}")
    typer.echo(
        f"Launch after review: uv run sprint env run {directory} --backend modal"
    )


@env_app.command("compile")
def compile_environment(
    directory: Annotated[Path, typer.Argument(help="Authored environment directory.")],
    output: Annotated[
        Path | None, typer.Option(help="Unused output directory.")
    ] = None,
):
    """Compile for inspection. Imports authored code; provisions no resources."""
    experiment = load_experiment(directory)
    target = output or directory / ".sprint" / ("compiled-" + uuid4().hex)
    experiment.compile(target)
    typer.echo(f"Compiled for inspection: {target}")


@env_app.command("run")
def run_environment(
    directory: Annotated[Path, typer.Argument(help="Authored environment directory.")],
    backend: Annotated[
        str, typer.Option(help="Learner backend: docker, modal or allocated.")
    ] = "docker",
    output: Annotated[
        Path | None, typer.Option(help="Unused output directory.")
    ] = None,
    trials: Annotated[int, typer.Option(min=1)] = 1,
    parallel: Annotated[int, typer.Option(min=1)] = 1,
):
    """Run the definition through Experiment.run; retain all standard records."""
    experiment = load_experiment(directory)
    agent = experiment.agent
    if agent.auth == "auto" and agent.harness in {"codex", "bounded-codex"}:
        auth_path = os.environ.get("CODEX_AUTH_JSON_PATH")
        forced = parse_bool_env_value(
            os.environ.get("CODEX_FORCE_AUTH_JSON"),
            name="CODEX_FORCE_AUTH_JSON",
            default=False,
        )
        if auth_path or forced or not os.environ.get("OPENAI_API_KEY", "").strip():
            auth = Path(auth_path) if auth_path else Path.home() / ".codex/auth.json"
            if not auth.is_file():
                raise ValueError(
                    "Run codex login or configure Agent(auth='api-key') first"
                )
            os.environ["CODEX_AUTH_JSON_PATH"] = str(auth)
    target = output or directory / ".sprint" / ("run-" + uuid4().hex)
    result = asyncio.run(
        experiment.run(
            directory=target, backend=backend, trials=trials, parallel=parallel
        )
    )
    typer.echo(f"Rewards: {result.rewards}")
    typer.echo(f"Records: {target}")
    for trial in result.trials:
        typer.echo(f"{trial.name}: {trial.status}")
    if len(result.trials) != trials or any(
        trial.status not in {"graded", "disqualified"} for trial in result.trials
    ):
        raise typer.Exit(1)
    if (result.directory / "cleanup-errors.json").exists():
        typer.echo("Resource cleanup remains unresolved; inspect cleanup-errors.json.")
        raise typer.Exit(1)
