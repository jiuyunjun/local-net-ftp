from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for instance in ("A", "B"):
        subprocess.Popen(
            [sys.executable, "-m", "localnetftp", "--dev-instance", instance],
            cwd=project_root,
            creationflags=creationflags,
        )
    print("已启动 LocalNetFTP A/B 开发双开实例。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
