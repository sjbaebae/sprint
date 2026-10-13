"""Exact retrieval and access boundaries for operator-held evidence."""

import pytest

from sprint.evidence import access, overview


def test_overview_preserves_trace_and_pages_original_workspace_text():
    text = "training evidence " * 3000
    packet = {
        "trace": [{"item": {"text": text}}],
        "workspace": {"files": [{"path": "code/train.py", "text": text}]},
    }
    view = overview(packet, external_sections={"workspace"})
    assert view["trace"] == packet["trace"]
    reference = view["workspace"]["files"][0]["text"]["read_evidence"]
    parts = []
    offset = 0
    while True:
        page = access(
            packet, "read_evidence", {"reference": reference, "offset": offset}
        )
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(parts) == text
    assert packet["workspace"]["files"][0]["text"] == text


def test_reader_rejects_files_commands_and_unbounded_reads():
    packet = {"a/b~c": ["evidence"]}
    reference = "packet#/a~1b~0c/0"
    assert (
        access(packet, "read_evidence", {"reference": reference})["content"]
        == "evidence"
    )
    listing = access(packet, "list_evidence", {"reference": "packet#/a~1b~0c"})
    assert listing["entries"][0]["reference"] == reference
    for tool, arguments in [
        ("read_evidence", {"reference": "/etc/passwd"}),
        ("read_evidence", {"reference": reference, "limit": 16001}),
        ("read_evidence", {"reference": reference, "offset": -1}),
        ("shell", {"reference": reference}),
    ]:
        with pytest.raises(ValueError):
            access(packet, tool, arguments)
