import sys

from scripts import build_exe


def test_build_exe_uses_nuitka(monkeypatch):
    captured = {}

    def fake_call(command, cwd):
        captured["command"] = command
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr(build_exe.subprocess, "call", fake_call)

    assert build_exe.main() == 0

    command = captured["command"]
    assert command[:3] == [sys.executable, "-m", "nuitka"]
    assert "--onefile" in command
    assert "--standalone" in command
    assert "--enable-plugin=pyside6" in command
    assert "--include-windows-runtime-dlls=auto" in command
    assert "--include-package=flask" in command
    assert "--include-package=iroh" in command
    assert any(arg.startswith("--include-data-files=") and "iroh_ffi.dll" in arg for arg in command)
    assert "--include-package=werkzeug" in command
    assert "--output-filename=LocalNetFTP.exe" in command
    assert command[-1] == "src/localnetftp/__main__.py"
