from pathlib import Path

import pytest

from localnetftp.transfer import safe_destination_path, scan_transfer_items


def test_scan_transfer_items_includes_files_and_directories(tmp_path):
    folder = tmp_path / "folder"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    file_path = nested / "a.txt"
    file_path.write_text("hello", encoding="utf-8")

    items = scan_transfer_items([folder])

    assert [(item.relative_path, item.is_dir, item.size) for item in items] == [
        ("folder", True, 0),
        ("folder/nested", True, 0),
        ("folder/nested/a.txt", False, 5),
    ]


def test_scan_transfer_items_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_transfer_items([tmp_path / "missing.txt"])


def test_safe_destination_path_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="Unsafe"):
        safe_destination_path(tmp_path, "../outside.txt")

    with pytest.raises(ValueError, match="Unsafe"):
        safe_destination_path(tmp_path, "/outside.txt")


def test_safe_destination_path_allows_nested_relative_path(tmp_path):
    assert safe_destination_path(tmp_path, "folder/a.txt") == (tmp_path / "folder" / "a.txt").resolve()
