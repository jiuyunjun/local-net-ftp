from __future__ import annotations

from pathlib import Path
from typing import Protocol


class LocalFileUrl(Protocol):
    def isLocalFile(self) -> bool: ...

    def toLocalFile(self) -> str: ...


def local_paths_from_urls(urls: list[LocalFileUrl]) -> list[Path]:
    paths: list[Path] = []
    for url in urls:
        if not url.isLocalFile():
            continue

        local_file = url.toLocalFile()
        if local_file:
            paths.append(Path(local_file))

    return paths


def append_unique_paths(existing: list[Path], incoming: list[Path]) -> list[Path]:
    known = {_normalize_path(path) for path in existing}
    merged = list(existing)

    for path in incoming:
        normalized = _normalize_path(path)
        if normalized in known:
            continue
        known.add(normalized)
        merged.append(path)

    return merged


def _normalize_path(path: Path) -> str:
    return str(path).casefold()
