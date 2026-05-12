from localnetftp.share import download_server
from localnetftp.share.download_server import (
    _decode_windows_command_output,
    _normalize_interface_name,
    _windows_ipconfig_interfaces,
    lan_download_urls,
    local_ipv4_interfaces,
)
from localnetftp.share import DownloadShareServer, MobileFileShareServer, MobileReceiveServer
import io
import socket
import subprocess
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
    assert "hello" in html
    assert "复制文字" in html
    assert "function copyText" in html
    assert "function isSafari()" in html
    assert data == b"updated"


def test_mobile_file_share_server_previews_images(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"png-bytes")
    port = _free_port()
    server = MobileFileShareServer([image], port=port, host="127.0.0.1")
    server.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        preview = urllib.request.urlopen(f"http://127.0.0.1:{port}/preview/1", timeout=5).read()
    finally:
        server.stop()

    assert '<img src="/preview/1"' in html
    assert preview == b"png-bytes"


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


def test_mobile_receive_server_saves_uploaded_file_and_text(tmp_path):
    received = []
    port = _free_port()
    server = MobileReceiveServer(tmp_path, port=port, host="127.0.0.1", on_received=received.append)
    server.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        response = _post_multipart(
            f"http://127.0.0.1:{port}/upload",
            fields={"text": "hello from phone"},
            files={"files": ("photo.png", b"image-bytes", "image/png")},
        )
    finally:
        server.stop()

    assert "发送到电脑" in html
    assert response["status"] == 200
    assert (tmp_path / "photo.png").read_bytes() == b"image-bytes"
    text_files = sorted(tmp_path.glob("手机文字_*.txt"))
    assert len(text_files) == 1
    assert text_files[0].read_text(encoding="utf-8") == "hello from phone"
    assert received == [[tmp_path / "photo.png", text_files[0]]]


def test_mobile_receive_server_avoids_uploaded_file_name_conflicts(tmp_path):
    existing = tmp_path / "same.txt"
    existing.write_text("old", encoding="utf-8")
    received = []
    port = _free_port()
    server = MobileReceiveServer(tmp_path, port=port, host="127.0.0.1", on_received=received.append)
    server.start()
    try:
        response = _post_multipart(
            f"http://127.0.0.1:{port}/upload",
            fields={},
            files={"files": ("../same.txt", b"new", "text/plain")},
        )
    finally:
        server.stop()

    assert response["status"] == 200
    assert existing.read_text(encoding="utf-8") == "old"
    saved_paths = list(tmp_path.glob("same_*.txt"))
    assert len(saved_paths) == 1
    assert saved_paths[0].read_bytes() == b"new"
    assert received == [[saved_paths[0]]]


def test_normalize_interface_name_repairs_ethernet_question_marks():
    assert _normalize_interface_name("Ethernet adapter ??? 7") == "以太网 7"
    assert _normalize_interface_name("Ethernet adapter Ethernet 2") == "以太网 Ethernet 2"


def test_decode_windows_command_output_uses_locale_encoding(monkeypatch):
    monkeypatch.setattr("localnetftp.share.download_server.locale.getpreferredencoding", lambda _: "cp932")

    assert _decode_windows_command_output("イーサネット".encode("cp932")) == "イーサネット"


def test_windows_ipconfig_interfaces_returns_empty_on_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr("localnetftp.share.download_server.subprocess.check_output", timeout)

    assert _windows_ipconfig_interfaces() == []


def test_local_ipv4_interfaces_caches_ipconfig_result(monkeypatch):
    monkeypatch.setattr(download_server, "_LOCAL_IPV4_INTERFACES_CACHE", None)
    calls = 0

    def fake_ipconfig():
        nonlocal calls
        calls += 1
        return [("イーサネット", "192.168.1.10")]

    monkeypatch.setattr(download_server, "_windows_ipconfig_interfaces", fake_ipconfig)

    assert local_ipv4_interfaces() == [("イーサネット", "192.168.1.10")]
    assert local_ipv4_interfaces() == [("イーサネット", "192.168.1.10")]
    assert calls == 1


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post_multipart(url: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> dict[str, object]:
    boundary = "LocalNetFTPTestBoundary"
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode("ascii"))
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.write(value.encode("utf-8"))
        body.write(b"\r\n")
    for name, (filename, content, content_type) in files.items():
        body.write(f"--{boundary}\r\n".encode("ascii"))
        body.write(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.write(content)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode("ascii"))
    request = urllib.request.Request(
        url,
        data=body.getvalue(),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return {"status": response.status, "body": response.read()}
