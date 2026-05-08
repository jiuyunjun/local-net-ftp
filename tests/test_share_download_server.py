from localnetftp.share.download_server import _normalize_interface_name, lan_download_urls
from localnetftp.share import DownloadShareServer, MobileFileShareServer
import io
import socket
import urllib.request
import zipfile


def test_lan_download_urls_builds_links_for_addresses():
    urls = lan_download_urls(49300, "LocalNetFTP.exe", ["192.168.1.10", "10.0.0.5"])

    assert [(item.interface_name, item.address, item.url) for item in urls] == [
        ("局域网", "192.168.1.10", "http://192.168.1.10:49300/"),
        ("局域网", "10.0.0.5", "http://10.0.0.5:49300/"),
    ]


def test_download_share_server_serves_html_button_and_file(tmp_path):
    exe_path = tmp_path / "LocalNetFTP.exe"
    exe_path.write_bytes(b"MZtest")
    port = _free_port()
    server = DownloadShareServer(exe_path, port=port, host="127.0.0.1")
    server.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        data = urllib.request.urlopen(f"http://127.0.0.1:{port}/download", timeout=5).read()
    finally:
        server.stop()

    assert "下载 Windows 版" in html
    assert data == b"MZtest"


def test_mobile_file_share_server_serves_original_files(tmp_path):
    first = tmp_path / "a.txt"
    first.write_text("hello", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    second = folder / "b.txt"
    second.write_text("world", encoding="utf-8")
    port = _free_port()
    server = MobileFileShareServer([first, folder], port=port, host="127.0.0.1")
    server.start()
    try:
        second.write_text("updated", encoding="utf-8")
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        data = urllib.request.urlopen(f"http://127.0.0.1:{port}/download/2", timeout=5).read()
    finally:
        server.stop()

    assert "选择文件下载" in html
    assert "a.txt" in html
    assert "b.txt" in html
    assert "下载选中" in html
    assert "打包 ZIP" in html
    assert "function isSafari()" in html
    assert data == b"updated"


def test_mobile_file_share_server_serves_selected_zip(tmp_path):
    first = tmp_path / "same.txt"
    first.write_text("first", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    second = folder / "same.txt"
    second.write_text("second", encoding="utf-8")
    port = _free_port()
    server = MobileFileShareServer([first, folder], port=port, host="127.0.0.1")
    server.start()
    try:
        data = urllib.request.urlopen(f"http://127.0.0.1:{port}/download.zip?ids=1,2", timeout=5).read()
    finally:
        server.stop()

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["same.txt", "same_2.txt"]
        assert archive.read("same.txt") == b"first"
        assert archive.read("same_2.txt") == b"second"


def test_normalize_interface_name_repairs_ethernet_question_marks():
    assert _normalize_interface_name("Ethernet adapter ??? 7") == "以太网 7"
    assert _normalize_interface_name("Ethernet adapter Ethernet 2") == "以太网 Ethernet 2"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
