from localnetftp.app import main


def test_main_returns_success(capsys):
    assert main() == 0
    assert "scaffold is ready" in capsys.readouterr().out
