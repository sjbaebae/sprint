"""Review highlights must resolve exact evidence and escape source HTML."""

import pytest

from sprint.failure_annotations import highlight, notes, validate_annotations


def test_annotation_is_exact_and_rendered_as_escaped_highlight():
    text = "<script>wrong</script>"
    events = [{"item": {"text": text}}]
    finding = {
        "id": "F1",
        "title": "Unsupported claim",
        "axis": "causal_reasoning",
        "points": 2,
        "failure": "Unsupported conclusion",
        "impact": "Invalid selection",
        "severity_reason": "Consequential claim",
        "evidence": ["packet#/trace/0"],
        "region": {"event": 1, "field": "item.text", "quote": text},
    }
    spans = validate_annotations(events, [finding])
    rendered = highlight(text, 1, "item.text", spans)
    assert "<mark" in rendered and "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "Causal Reasoning — Unsupported claim" in notes([finding], 1)
    finding["region"]["quote"] = "invented"
    with pytest.raises(ValueError, match="uniquely"):
        validate_annotations(events, [finding])
