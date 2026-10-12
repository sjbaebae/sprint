from sprint.workspace import workspace_evidence


def test_workspace_records_budget_and_symlinks(tmp_path):
    (tmp_path / "code.py").write_text("x=1")
    outside = tmp_path.parent / "operator-secret.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    (tmp_path / "data.bin").write_bytes(b"\xff")
    result = workspace_evidence(tmp_path, content_budget=3)
    files = {entry["path"]: entry for entry in result["files"]}
    assert files["code.py"]["text"] == "x=1"
    assert files["link"] == {"path": "link", "omitted": "symlink"}
    assert "text" not in files["data.bin"]
    assert result["content_bytes"] == 3


def test_excluded_directories_do_not_consume_evidence_budget(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret").write_text("secret")
    (tmp_path / "report.md").write_text("measured outcome")
    result = workspace_evidence(tmp_path, exclude_dirs=("private",))
    assert [entry["path"] for entry in result["files"]] == ["report.md"]
