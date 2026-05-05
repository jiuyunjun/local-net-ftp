import localnetftp.app


def test_main_runs_tray_app(monkeypatch):
    monkeypatch.setattr(localnetftp.app, "run_tray_app", lambda: 0)

    assert localnetftp.app.main() == 0
