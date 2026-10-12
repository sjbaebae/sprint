"""Repeated verifier execution must never preserve an earlier finalized reward."""

import json

import pytest

import sprint.validation as validation


@pytest.mark.parametrize(
    "failure",
    ["bad-json", "missing-config", "missing-module", "import-error", "invalid-score"],
)
def test_failed_regrading_clears_prior_reward_and_records_quarantine(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "reward.json").write_text('{"reward": 1.0}')
    (output / "result.json").write_text('{"status":"graded","reward":1.0}')
    monkeypatch.setattr(validation, "OUTPUT", output)
    # The standalone entrypoint adds the bundle to sys.path; restore it per test.
    monkeypatch.setattr(validation.sys, "path", list(validation.sys.path))
    config = tmp_path / "config.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    if failure == "bad-json":
        config.write_text("{broken")
    elif failure != "missing-config":
        config.write_text(
            json.dumps(
                {
                    "entrypoint": "evaluate.py",
                    "rubric": None,
                    "submission": str(tmp_path / "submission"),
                    "weights": [1.0, 0.0],
                }
            )
        )
        if failure == "import-error":
            (bundle / "evaluate.py").write_text(
                'raise RuntimeError("grader import failed")'
            )
        elif failure == "invalid-score":
            (bundle / "evaluate.py").write_text(
                'def verify(context): return {"score": True}'
            )
    with pytest.raises(SystemExit) as error:
        validation.main(config)
    assert error.value.code == 2
    assert not (output / "reward.json").exists()
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "quarantined"
    assert result["error_type"]
    assert "reward" not in result


def test_successful_regrading_replaces_previous_outputs(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    (output / "reward.json").write_text('{"reward": 1.0}')
    (output / "result.json").write_text('{"status":"graded","reward":1.0}')
    monkeypatch.setattr(validation, "OUTPUT", output)
    monkeypatch.setattr(validation.sys, "path", list(validation.sys.path))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "evaluate.py").write_text('def verify(context): return {"score": 0.25}')
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "entrypoint": "evaluate.py",
                "rubric": None,
                "submission": str(tmp_path / "submission"),
                "weights": [1.0, 0.0],
            }
        )
    )
    validation.main(config)
    assert json.loads((output / "reward.json").read_text()) == {"reward": 0.25}
    assert json.loads((output / "result.json").read_text())["reward"] == 0.25
