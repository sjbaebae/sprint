"""Reject invalid limits at definition time, before staging or provisioning."""

from pathlib import Path

import pytest

from sprint import Budget, Validation, Worker


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), "10", -1, 0, 0.5]
)
@pytest.mark.parametrize(
    "factory", [Budget, lambda **kwargs: Validation(Path("private"), **kwargs)]
)
def test_time_limits_require_positive_integer_seconds(factory, value):
    with pytest.raises(ValueError, match="positive integer"):
        factory(seconds=value)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "10", -1])
def test_cost_limits_require_finite_nonnegative_numbers(value):
    with pytest.raises(ValueError, match="finite"):
        Budget(worker_spend=value)
    with pytest.raises(ValueError, match="finite"):
        Worker(hourly_cost=value)
