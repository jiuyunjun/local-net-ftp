from localnetftp.ui.tray_app import _is_image_preview_path, _is_text_preview_path, _read_text_preview
import pytest
from localnetftp.ui import tray_app


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


@pytest.mark.parametrize("name", ["space name.txt", "中文,测试.txt", "received folder"])
def test_open_save_location_opens_actual_parent(tmp_path, monkeypatch, name):
    folder = tmp_path / "保存 目录,测试"
    folder.mkdir()
    item = folder / name
    if name == "received folder":
        item.mkdir()
    else:
        item.write_text("received", encoding="utf-8")
    opened = []
    monkeypatch.setattr(tray_app, "_open_path", opened.append)
    tray_app._open_save_location([item])
    assert opened == [folder.resolve()]


def test_open_save_location_works_after_file_deleted(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(tray_app, "_open_path", opened.append)
    tray_app._open_save_location([tmp_path / "deleted.txt"])
    assert opened == [tmp_path.resolve()]
    with pytest.raises(FileNotFoundError):
        tray_app._open_save_location([tmp_path / "deleted-folder" / "file.txt"])


def test_preview_bounds_read_and_preserves_utf8_boundary(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_text("中文" * 1000, encoding="utf-8")
    original = type(path).open
    reads = []
    class LimitedRead:
        def __enter__(self):
            self.stream = original(path, "rb")
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 6
            return self.stream.read(size)
    monkeypatch.setattr(type(path), "open", lambda *_args, **_kwargs: LimitedRead())
    assert _read_text_preview(path, max_bytes=5) == "中\n..."
    assert reads == [6]
