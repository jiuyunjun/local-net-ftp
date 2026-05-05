from pathlib import Path

from localnetftp.ui.drop_paths import append_unique_paths, local_paths_from_urls


class FakeUrl:
    def __init__(self, local_file: str, is_local: bool = True) -> None:
        self._local_file = local_file
        self._is_local = is_local

    def isLocalFile(self) -> bool:
        return self._is_local

    def toLocalFile(self) -> str:
        return self._local_file


def test_local_paths_from_urls_keeps_only_local_files():
    paths = local_paths_from_urls(
        [
            FakeUrl(r"C:\Users\A\Downloads\a.txt"),
            FakeUrl("https://example.com/a.txt", is_local=False),
            FakeUrl(""),
        ]
    )

    assert paths == [Path(r"C:\Users\A\Downloads\a.txt")]


def test_append_unique_paths_preserves_order_and_skips_duplicates():
    existing = [Path(r"C:\Share\a.txt")]
    incoming = [
        Path(r"C:\Share\b.txt"),
        Path(r"c:\share\A.txt"),
        Path(r"C:\Share\folder"),
    ]

    assert append_unique_paths(existing, incoming) == [
        Path(r"C:\Share\a.txt"),
        Path(r"C:\Share\b.txt"),
        Path(r"C:\Share\folder"),
    ]
