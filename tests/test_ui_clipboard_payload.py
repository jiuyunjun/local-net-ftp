from datetime import datetime

from localnetftp.ui.clipboard_payload import timestamped_clipboard_path


def test_timestamped_clipboard_path_uses_millisecond_timestamp(tmp_path):
    path = timestamped_clipboard_path(tmp_path, ".png", datetime(2026, 5, 6, 1, 2, 3, 456000))

    assert path == tmp_path / "20260506010203456.png"
