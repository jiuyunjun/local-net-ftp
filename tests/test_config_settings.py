import json
from pathlib import Path

import pytest

from localnetftp.config import (
    APP_NAME,
    AppConfig,
    default_config_dir,
    default_download_dir,
    load_config,
    save_config,
)


def test_default_download_dir_uses_windows_downloads_name():
    assert default_download_dir(Path("C:/Users/A")) == Path("C:/Users/A/Downloads")


def test_default_config_dir_uses_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\A\AppData\Roaming")

    assert default_config_dir() == Path(r"C:\Users\A\AppData\Roaming") / APP_NAME


def test_load_config_returns_defaults_when_file_is_missing(tmp_path):
    config = load_config(tmp_path / "missing.json")

    assert config == AppConfig(receive_dir=default_download_dir())


def test_save_and_load_config_round_trip(tmp_path):
    config_path = tmp_path / "config.json"
    expected = AppConfig(receive_dir=tmp_path / "Downloads", start_on_boot=True)

    saved_path = save_config(expected, config_path)
    loaded = load_config(config_path)

    assert saved_path == config_path
    assert loaded == expected
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "receive_dir": str(tmp_path / "Downloads"),
        "start_on_boot": True,
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
