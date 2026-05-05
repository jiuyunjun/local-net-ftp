from pathlib import Path

from localnetftp.ui.send_state import can_send, confirmation_text, send_summary


def test_can_send_requires_peer_and_path():
    assert can_send(selected_peer_count=1, pending_path_count=1) is True
    assert can_send(selected_peer_count=0, pending_path_count=1) is False
    assert can_send(selected_peer_count=1, pending_path_count=0) is False


def test_send_summary_mentions_counts_and_peer_names():
    summary = send_summary(["A-PC", "B-PC"], [Path("a.txt"), Path("folder")])

    assert summary == "准备发送 2 个项目给 2 个用户：A-PC、B-PC"


def test_confirmation_text_lists_recipients_and_paths():
    text = confirmation_text(["A-PC"], [Path("a.txt"), Path("folder")])

    assert "发送给：A-PC" in text
    assert "- a.txt" in text
    assert "- folder" in text
