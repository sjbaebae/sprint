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
    assert grade_axes(
        criteria, {"T:4": {}, "R:8": {}}, expected_ids=["design", "implementation"]
    )["score"] == pytest.approx(5 / 6)
    with pytest.raises(ValueError, match="resolvable"):
        grade_axes(criteria, {"T:4": {}}, expected_ids=["design", "implementation"])
    criteria[0]["explanation"] = ""
    with pytest.raises(ValueError, match="explanation"):
        grade_axes(
            criteria, {"T:4": {}, "R:8": {}}, expected_ids=["design", "implementation"]
        )


def test_judge_cannot_omit_or_invent_rubric_axes():
    criteria = [
        {
            "id": "design",
            "score": 3,
            "explanation": "Controlled comparison.",
            "evidence": ["T:4"],
        }
    ]
    with pytest.raises(ValueError, match="exactly the expected"):
        grade_axes(criteria, {"T:4": {}}, expected_ids=["design", "implementation"])
    with pytest.raises(ValueError, match="exactly the expected"):
        grade_axes(criteria, {"T:4": {}}, expected_ids=["hypotheses"])


@pytest.mark.parametrize(
    "violation",
    [
        {"id": "", "evidence": ["T:1"]},
        {"id": "claim", "evidence": "T:1"},
        {"id": "claim", "evidence": []},
        {"id": "claim", "evidence": [None]},
        {"id": "claim", "evidence": [False]},
        {"id": "claim", "evidence": [""]},
        {"id": "claim", "evidence": ["   "]},
        {"id": "claim", "evidence": [{}]},
    ],
)
def test_violation_records_require_ids_and_evidence_lists(violation):
    from sprint.validation import validate_violation_groups

    with pytest.raises(ValueError, match="evidence list"):
        validate_violation_groups({"confirmed": [violation], "suspected": []})


@pytest.mark.parametrize("same_group", [False, True])
def test_duplicate_or_conflicting_violation_ids_are_not_finalizable(same_group):
    from sprint.validation import validate_violation_groups

    violation = {"id": "protected-data-use", "evidence": ["T:8"]}
    value = {"confirmed": [violation], "suspected": []}
    value["confirmed" if same_group else "suspected"].append(violation)
    with pytest.raises(ValueError, match="unique across"):
        validate_violation_groups(value)


def test_violation_evidence_allows_structured_observations():
    from sprint.validation import validate_violation_groups

    value = {
        "confirmed": [
            {
                "id": "dataset-overlap",
                "evidence": [{"row_hash": "abc", "source": "trusted-overlap-check"}],
            }
        ],
        "suspected": [],
    }
    assert validate_violation_groups(value) == value
