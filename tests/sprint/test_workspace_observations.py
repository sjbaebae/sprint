"""Core bounds and socket decoding for operator workspace observations."""

from sprint.workspace_observations import collect


def test_bounded_scans_advance_then_wrap(tmp_path):
    (tmp_path / "a.txt").write_text("first!")
    (tmp_path / "b.txt").write_text("second")
    first = collect(tmp_path, max_bytes=8)
    assert [row["path"] for row in first["files"]] == ["/a.txt"]
    assert first["next_cursor"] == "/a.txt"
    second = collect(tmp_path, max_bytes=8, start_after=first["next_cursor"])
    assert [row["path"] for row in second["files"]] == ["/b.txt"]
    assert second["next_cursor"] == ""
    assert second["complete"] is False


def test_network_snapshot_filters_uid_and_decodes_address(tmp_path):
    from sprint.workspace_observations import network_snapshot

    header = "sl local_address rem_address st tx_queue rx_queue tr uid\n"
    row = "0: 0100007F:C350 08080808:01BB 01 0:0 0:0 0 1000 0 0\n"
    for protocol in ("tcp", "tcp6", "udp", "udp6"):
        (tmp_path / protocol).write_text(header + (row if protocol == "tcp" else ""))
    result = network_snapshot(tmp_path)
    assert result["connections"][0]["remote"] == {"address": "8.8.8.8", "port": 443}
    assert not result["errors"]
