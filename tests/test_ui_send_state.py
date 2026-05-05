from pathlib import Path

from localnetftp.ui.send_state import can_send, send_summary


def test_can_send_requires_peer_and_path():
    assert can_send(selected_peer_count=1, pending_path_count=1) is True
    assert can_send(selected_peer_count=0, pending_path_count=1) is False
    assert can_send(selected_peer_count=1, pending_path_count=0) is False


def test_send_summary_mentions_counts_and_peer_names():
    summary = send_summary(["A-PC", "B-PC"], [Path("a.txt"), Path("folder")])

    assert summary == "准备发送 2 个项目给 2 个用户：A-PC、B-PC"
