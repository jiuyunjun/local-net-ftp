from __future__ import annotations

import argparse
import sys

from localnetftp.ui.tray_app import RuntimeOptions, dev_runtime_options, run_tray_app


def main(argv: list[str] | None = None) -> int:
    return run_tray_app(_parse_runtime_options(sys.argv[1:] if argv is None else argv))


def _parse_runtime_options(argv: list[str]) -> RuntimeOptions:
    parser = argparse.ArgumentParser(prog="localnetftp")
    parser.add_argument(
        "--dev-instance",
        help="Start an isolated local development instance, for example A or B.",
    )
    args = parser.parse_args(argv)
    if args.dev_instance:
        return dev_runtime_options(args.dev_instance)
    return RuntimeOptions()
