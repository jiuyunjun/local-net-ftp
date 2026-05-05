import json
from pathlib import Path

import pytest

from localnetftp.config import (
    APP_NAME,
    AppConfig,
    default_config_dir,
    default_device_id,
    default_device_name,
    default_download_dir,
    load_config,
    save_config,
)


def test_default_download_dir_uses_windows_downloads_name():
    assert default_download_dir(Path("C:/Users/A")) == Path("C:/Users/A/Downloads")


def test_default_config_dir_uses_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\A\AppData\Roaming")

    assert default_config_dir() == Path(r"C:\Users\A\AppData\Roaming") / APP_NAME


def test_default_device_name_prefers_computer_name(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "A-PC")

    assert default_device_name() == "A-PC"


def test_default_device_name_falls_back_to_user_name(monkeypatch):
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.setattr("localnetftp.config.settings.getpass.getuser", lambda: "alice")

    assert default_device_name() == "alice PC"


def test_default_device_id_returns_uuid_string():
    assert len(default_device_id()) == 36


def test_load_config_returns_defaults_when_file_is_missing(tmp_path):
    config = load_config(tmp_path / "missing.json")

    assert config.receive_dir == default_download_dir()
    assert config.device_name == default_device_name()
    assert config.device_id


def test_save_and_load_config_round_trip(tmp_path):
    config_path = tmp_path / "config.json"
    expected = AppConfig(
        receive_dir=tmp_path / "Downloads",
        start_on_boot=True,
        device_name="A-PC",
        device_id="device-a",
    )

    saved_path = save_config(expected, config_path)
    loaded = load_config(config_path)

    assert saved_path == config_path
    assert loaded == expected
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "receive_dir": str(tmp_path / "Downloads"),
        "start_on_boot": True,
        "device_name": "A-PC",
        "device_id": "device-a",
    }


def test_load_config_rejects_invalid_receive_dir(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"receive_dir": ""}', encoding="utf-8")

    with pytest.raises(ValueError, match="receive_dir"):
        load_config(config_path)


def test_load_config_rejects_invalid_start_on_boot(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"start_on_boot": "yes"}', encoding="utf-8")

    with pytest.raises(ValueError, match="start_on_boot"):
        load_config(config_path)


def test_load_config_rejects_invalid_device_name(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"device_name": "   "}', encoding="utf-8")

    with pytest.raises(ValueError, match="device_name"):
        load_config(config_path)


def test_load_config_rejects_invalid_device_id(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"device_id": ""}', encoding="utf-8")

    with pytest.raises(ValueError, match="device_id"):
        load_config(config_path)
