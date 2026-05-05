from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from localnetftp.config.settings import APP_NAME


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command(executable_path: Path | str | None = None) -> str:
    path = Path(executable_path) if executable_path is not None else Path(sys.executable)
    return f'"{path}"'


def is_start_on_boot_enabled(
    executable_path: Path | str | None = None,
    *,
    app_name: str = APP_NAME,
    registry: Any | None = None,
) -> bool:
    winreg = _winreg(registry)
    expected_command = startup_command(executable_path)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            current_command, _ = winreg.QueryValueEx(key, app_name)
    except FileNotFoundError:
        return False

    return current_command == expected_command


def set_start_on_boot(
    enabled: bool,
    executable_path: Path | str | None = None,
    *,
    app_name: str = APP_NAME,
    registry: Any | None = None,
) -> None:
    winreg = _winreg(registry)
    access = winreg.KEY_SET_VALUE
    command = startup_command(executable_path)

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, access) as key:
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass


def _winreg(registry: Any | None) -> Any:
    if registry is not None:
        return registry

    if sys.platform != "win32":
        raise RuntimeError("Start-on-boot settings are only supported on Windows.")

    import winreg

    return winreg
