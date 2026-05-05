"""Configuration helpers for LocalNetFTP."""

from localnetftp.config.settings import (
    APP_NAME,
    CONFIG_FILE_NAME,
    AppConfig,
    default_config,
    default_config_dir,
    default_config_path,
    default_device_id,
    default_device_name,
    default_download_dir,
    load_config,
    save_config,
)
from localnetftp.config.autostart import (
    RUN_KEY_PATH,
    is_start_on_boot_enabled,
    set_start_on_boot,
    startup_command,
)

__all__ = [
    "APP_NAME",
    "CONFIG_FILE_NAME",
    "RUN_KEY_PATH",
    "AppConfig",
    "default_config",
    "default_config_dir",
    "default_config_path",
    "default_device_id",
    "default_device_name",
    "default_download_dir",
    "is_start_on_boot_enabled",
    "load_config",
    "save_config",
    "set_start_on_boot",
    "startup_command",
]
