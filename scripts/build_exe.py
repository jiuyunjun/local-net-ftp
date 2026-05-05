from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--onefile",
        "--windows-console-mode=disable",
        "--enable-plugin=pyside6",
        "--include-package=flask",
        "--include-package=iroh",
        "--include-package=werkzeug",
        "--include-package=jinja2",
        "--include-package=click",
        "--include-package=itsdangerous",
        "--include-package=blinker",
        "--assume-yes-for-downloads",
        "--output-dir=dist",
        "--output-filename=LocalNetFTP.exe",
        "--product-name=LocalNetFTP",
        "--file-description=LocalNetFTP",
        "--file-version=0.1.0",
        "--product-version=0.1.0",
        "--company-name=LocalNetFTP",
        "--remove-output",
        "src/localnetftp/__main__.py",
    ]
    return subprocess.call(command, cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
