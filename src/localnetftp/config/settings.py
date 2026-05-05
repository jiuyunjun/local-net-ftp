from __future__ import annotations

import json
import os
import getpass
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_NAME = "LocalNetFTP"
CONFIG_FILE_NAME = "config.json"


@dataclass(frozen=True)
class AppConfig:
    receive_dir: Path
    start_on_boot: bool = False
    device_name: str = ""

    def to_json_data(self) -> dict[str, Any]:
        data = asdict(self)
        data["receive_dir"] = str(self.receive_dir)
        return data


def default_download_dir(home: Path | None = None) -> Path:
    user_home = home or Path.home()
    return user_home / "Downloads"


def default_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def default_config_path() -> Path:
    return default_config_dir() / CONFIG_FILE_NAME


def default_config() -> AppConfig:
    return AppConfig(receive_dir=default_download_dir(), device_name=default_device_name())


def default_device_name() -> str:
    computer_name = os.environ.get("COMPUTERNAME")
    if computer_name and computer_name.strip():
        return computer_name.strip()

    user_name = getpass.getuser()
    if user_name and user_name.strip():
        return f"{user_name.strip()} PC"

    return "LocalNetFTP PC"


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        return default_config()

    raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    return _config_from_json_data(raw_data)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config.to_json_data(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


def _config_from_json_data(raw_data: object) -> AppConfig:
    if not isinstance(raw_data, dict):
        raise ValueError("Config file must contain a JSON object.")

    receive_dir = raw_data.get("receive_dir")
    if receive_dir is None:
        receive_path = default_download_dir()
    elif isinstance(receive_dir, str) and receive_dir.strip():
        receive_path = Path(receive_dir).expanduser()
    else:
        raise ValueError("Config field 'receive_dir' must be a non-empty string.")

    start_on_boot = raw_data.get("start_on_boot", False)
    if not isinstance(start_on_boot, bool):
        raise ValueError("Config field 'start_on_boot' must be a boolean.")

    device_name = raw_data.get("device_name")
    if device_name is None:
        display_name = default_device_name()
    elif isinstance(device_name, str) and device_name.strip():
        display_name = device_name.strip()
    else:
        raise ValueError("Config field 'device_name' must be a non-empty string.")

    return AppConfig(
        receive_dir=receive_path,
        start_on_boot=start_on_boot,
        device_name=display_name,
    )
