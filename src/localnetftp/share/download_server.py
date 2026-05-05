from __future__ import annotations

import socket
import subprocess
import threading
from dataclasses import dataclass
from html import escape
from pathlib import Path

from flask import Flask, send_file
from werkzeug.serving import make_server


DEFAULT_SHARE_PORT = 49300


@dataclass(frozen=True)
class ShareAddress:
    interface_name: str
    address: str
    url: str


def lan_download_urls(port: int, filename: str, addresses: list[str] | None = None) -> list[ShareAddress]:
    interfaces = (
        [("局域网", address) for address in addresses]
        if addresses is not None
        else local_ipv4_interfaces()
    )
    return [
        ShareAddress(interface_name=interface_name, address=address, url=f"http://{address}:{port}/")
        for interface_name, address in interfaces
    ]


def local_ipv4_addresses() -> list[str]:
    return [address for _, address in local_ipv4_interfaces()]


def local_ipv4_interfaces() -> list[tuple[str, str]]:
    ipconfig_interfaces = _windows_ipconfig_interfaces()
    if ipconfig_interfaces:
        return ipconfig_interfaces

    addresses: set[str] = set()
    host_name = socket.gethostname()

    try:
        for result in socket.getaddrinfo(host_name, None, socket.AF_INET, socket.SOCK_STREAM):
            address = result[4][0]
            if _is_lan_address(address):
                addresses.add(address)
    except socket.gaierror:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if _is_lan_address(address):
                addresses.add(address)
    except OSError:
        pass

    return [("局域网", address) for address in sorted(addresses)]


class DownloadShareServer:
    def __init__(self, file_path: Path, port: int = DEFAULT_SHARE_PORT, host: str = "0.0.0.0") -> None:
        self.file_path = file_path
        self.port = port
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.file_path.exists():
            raise FileNotFoundError(self.file_path)

        app = Flask(__name__)

        @app.get("/")
        def index():
            filename = escape(self.file_path.name)
            return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalNetFTP 下载</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background: #f4f7fb;
      color: #172033;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      padding: 28px;
      border: 1px solid #d8e0ea;
      border-radius: 14px;
      background: white;
      box-shadow: 0 18px 50px rgba(31, 45, 61, 0.12);
      text-align: center;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}
    p {{
      margin: 0 0 22px;
      color: #5b687a;
    }}
    a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 0 20px;
      border-radius: 8px;
      background: #2f7dd1;
      color: white;
      text-decoration: none;
      font-weight: 600;
    }}
    a:hover {{
      background: #2269b5;
    }}
  </style>
</head>
<body>
  <main>
    <h1>LocalNetFTP</h1>
    <p>{filename}</p>
    <a href="/download">下载 Windows 版</a>
  </main>
</body>
</html>"""

        @app.get("/download")
        def download():
            return send_file(self.file_path, as_attachment=True, download_name=self.file_path.name)

        self._server = make_server(self.host, self.port, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalNetFTPDownloadShare",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def urls(self) -> list[ShareAddress]:
        return lan_download_urls(self.port, self.file_path.name)


def _is_lan_address(address: str) -> bool:
    return not address.startswith("127.") and not address.startswith("169.254.")


def _windows_ipconfig_interfaces() -> list[tuple[str, str]]:
    try:
        output = subprocess.check_output(
            ["ipconfig"],
            text=True,
            encoding="gbk",
            errors="ignore",
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    interfaces: list[tuple[str, str]] = []
    current_name = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if raw_line and not raw_line.startswith(" ") and line.endswith(":"):
            current_name = line.rstrip(":")
            continue
        if "IPv4" not in line:
            continue
        _, _, value = line.partition(":")
        address = value.strip()
        if address and _is_lan_address(address):
            interfaces.append((current_name or "局域网", address))

    return interfaces
