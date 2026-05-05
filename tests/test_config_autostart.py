from pathlib import Path

from localnetftp.config import (
    APP_NAME,
    RUN_KEY_PATH,
    is_start_on_boot_enabled,
    set_start_on_boot,
    startup_command,
)


class FakeKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert path == RUN_KEY_PATH
        assert reserved == 0
        assert access == self.KEY_READ
        return FakeKey(self)

    def CreateKeyEx(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert path == RUN_KEY_PATH
        assert reserved == 0
        assert access == self.KEY_SET_VALUE
        return FakeKey(self)

    def QueryValueEx(self, key, name):
        try:
            return self.values[name], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name)

    def SetValueEx(self, key, name, reserved, value_type, value):
        assert reserved == 0
        assert value_type == self.REG_SZ
        self.values[name] = value

    def DeleteValue(self, key, name):
        try:
            del self.values[name]
        except KeyError:
            raise FileNotFoundError(name)


def test_startup_command_quotes_executable_path():
    assert startup_command(Path(r"C:\Program Files\LocalNetFTP\LocalNetFTP.exe")) == (
        r'"C:\Program Files\LocalNetFTP\LocalNetFTP.exe"'
    )


def test_set_start_on_boot_writes_run_key():
    registry = FakeRegistry()
    executable = Path(r"C:\Apps\LocalNetFTP.exe")

    set_start_on_boot(True, executable, registry=registry)

    assert registry.values[APP_NAME] == r'"C:\Apps\LocalNetFTP.exe"'


def test_set_start_on_boot_false_removes_run_key():
    registry = FakeRegistry()
    executable = Path(r"C:\Apps\LocalNetFTP.exe")
    registry.values[APP_NAME] = startup_command(executable)

    set_start_on_boot(False, executable, registry=registry)

    assert APP_NAME not in registry.values


def test_set_start_on_boot_false_ignores_missing_value():
    registry = FakeRegistry()

    set_start_on_boot(False, Path(r"C:\Apps\LocalNetFTP.exe"), registry=registry)

    assert registry.values == {}


def test_is_start_on_boot_enabled_requires_matching_command():
    registry = FakeRegistry()
    executable = Path(r"C:\Apps\LocalNetFTP.exe")
    registry.values[APP_NAME] = startup_command(executable)

    assert is_start_on_boot_enabled(executable, registry=registry) is True
    assert is_start_on_boot_enabled(Path(r"C:\Other.exe"), registry=registry) is False


def test_is_start_on_boot_enabled_returns_false_when_missing():
    registry = FakeRegistry()

    assert is_start_on_boot_enabled(Path(r"C:\Apps\LocalNetFTP.exe"), registry=registry) is False
