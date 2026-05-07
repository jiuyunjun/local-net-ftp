from localnetftp.ui.tray_app import _is_image_preview_path, _is_text_preview_path, _read_text_preview


def test_received_preview_detects_images_and_text_files():
    from pathlib import Path

    assert _is_image_preview_path(Path("shot.PNG")) is True
    assert _is_image_preview_path(Path("note.txt")) is False
    assert _is_text_preview_path(Path("note.TXT")) is True
    assert _is_text_preview_path(Path("photo.png")) is False


def test_read_text_preview_supports_utf8(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("hello 中文", encoding="utf-8")

    assert _read_text_preview(path) == "hello 中文"
