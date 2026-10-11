import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sprint.job import Job
from sprint.models.job.config import JobConfig
from sprint.models.task.config import TaskConfig as TaskDefinitionConfig
from sprint.models.trial.config import TaskConfig, TrialConfig
from sprint.models.trial.paths import TrialPaths
from sprint.models.trial.result import AgentInfo
from sprint.trial.trial import Trial


pytestmark = pytest.mark.unit


async def test_job_run_writes_config_without_defaults(tmp_path):
    config = JobConfig(
        job_name="minimal-write",
        jobs_dir=tmp_path / "jobs",
        quiet=True,
    )
    job = Job(config, _task_configs=[], _metrics={}, _task_download_results={})

    await job.run()

    data = json.loads((job.job_dir / "config.json").read_text())
    assert data == {
        "job_name": "minimal-write",
        "jobs_dir": str(tmp_path / "jobs"),
        "quiet": True,
    }


def test_trial_init_result_writes_config_without_defaults(tmp_path):
    config = TrialConfig(
        task=TaskConfig(path=tmp_path / "task"),
        trial_name="minimal-trial",
        trials_dir=tmp_path / "trials",
    )
    paths = TrialPaths(trial_dir=config.trials_dir / config.trial_name)
    fake_trial = SimpleNamespace(
        _id=uuid4(),
        agent=SimpleNamespace(
            to_agent_info=lambda: AgentInfo(name="oracle", version="unknown")
        ),
        config=config,
        paths=paths,
        task=SimpleNamespace(
            name="task",
            checksum="abc123",
            has_steps=False,
            config=TaskDefinitionConfig(),
        ),
        _now=lambda: datetime.now(timezone.utc),
    )

    Trial._init_result(fake_trial)

    data = json.loads(paths.config_path.read_text())
    assert data == {
        "task": {"path": str(tmp_path / "task")},
        "trial_name": "minimal-trial",
        "trials_dir": str(tmp_path / "trials"),
    }
