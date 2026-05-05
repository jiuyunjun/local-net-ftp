from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = _parse_args()
    project_root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "localnetftp", "--dev-instance", args.instance]
    print(f"启动 LocalNetFTP debug 客户端: {args.instance}")
    print(" ".join(command))
    return subprocess.call(command, cwd=project_root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="start_debug_client")
    parser.add_argument(
        "instance",
        nargs="?",
        default="DEBUG",
        help="Debug instance name. Defaults to DEBUG; use A/B to join the dev pair.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
