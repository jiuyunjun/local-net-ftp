from __future__ import annotations

from datetime import datetime
from pathlib import Path


def timestamped_clipboard_path(directory: Path, suffix: str, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S%f")[:-3]
    return directory / f"{timestamp}{suffix}"
