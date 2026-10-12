"""Rubric scores require attributable evidence and concrete explanations."""

import pytest

from sprint.validation import grade_axes


def test_equal_weights_and_reference_resolution():
    criteria = [
        {
            "id": "design",
            "score": 2,
            "explanation": "Source proportions changed with filtering, limiting identification.",
            "evidence": ["T:4"],
        },
        {
            "id": "implementation",
            "score": 3,
            "explanation": "Cold load reproduced the measured outputs.",
            "evidence": ["R:8"],
        },
    ]
    assert grade_axes(criteria, {"T:4": {}, "R:8": {}})["score"] == pytest.approx(5 / 6)
    with pytest.raises(ValueError, match="resolvable"):
        grade_axes(criteria, {"T:4": {}})
    criteria[0]["explanation"] = ""
    with pytest.raises(ValueError, match="explanation"):
        grade_axes(criteria, {"T:4": {}, "R:8": {}})
