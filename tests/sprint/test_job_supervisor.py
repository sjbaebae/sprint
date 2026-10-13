"""A successful CLI process must not hide errored research trials."""

import json
from types import SimpleNamespace

import pytest
from sprint.job_supervisor import supervise


@pytest.mark.parametrize("errors, expected", [(0, "completed"), (1, "failed")])
def test_supervisor_uses_trial_results_not_only_exit_code(
    tmp_path, monkeypatch, errors, expected
):
    monkeypatch.chdir(tmp_path)
    result = tmp_path / "jobs" / "trial" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1, "n_errored_trials": errors},
            }
        )
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"jobs_dir": str(tmp_path / "jobs"), "job_name": "trial"})
    )
    monkeypatch.setattr(
        "sprint.job_supervisor.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    record = tmp_path / "lifecycle.json"
    supervise(config, record)
    retained = json.loads(record.read_text())
    assert retained["state"] == expected
    assert retained["exit_code"] == 0
    if errors:
        assert retained["failure"] == "trial_errors"
