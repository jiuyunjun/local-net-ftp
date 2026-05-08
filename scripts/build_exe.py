from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _iroh_dll_arg() -> str:
    """Place iroh_ffi.dll where iroh_ffi.py loads it from at runtime."""
    spec = importlib.util.find_spec("iroh")
    if spec is None or spec.origin is None:
        raise RuntimeError("iroh package not found")
    dll = Path(spec.origin).parent / "iroh_ffi.dll"
    if not dll.exists():
        raise FileNotFoundError(dll)
    return f"--include-data-files={dll}=iroh/iroh_ffi.dll"


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--onefile",
        "--windows-console-mode=disable",
        "--enable-plugin=pyside6",
        "--include-windows-runtime-dlls=auto",
        "--include-package=flask",
        "--include-package=iroh",
        "--include-package=qrcode",
        _iroh_dll_arg(),  # iroh_ffi.dll must be at iroh/ so iroh_ffi.py finds it
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
