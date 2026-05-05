import localnetftp.app


def test_main_runs_tray_app(monkeypatch):
    captured_options = []
    monkeypatch.setattr(localnetftp.app, "run_tray_app", lambda options: captured_options.append(options) or 0)

    assert localnetftp.app.main([]) == 0
    assert captured_options[0].dev_instance == ""


def test_main_accepts_dev_instance(monkeypatch):
    captured_options = []
    monkeypatch.setattr(localnetftp.app, "run_tray_app", lambda options: captured_options.append(options) or 0)

    assert localnetftp.app.main(["--dev-instance", "B"]) == 0
    assert captured_options[0].dev_instance == "B"
    assert captured_options[0].transfer_port == 49211
