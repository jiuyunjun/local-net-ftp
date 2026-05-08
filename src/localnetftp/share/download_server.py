from __future__ import annotations

import socket
import subprocess
import threading
from dataclasses import dataclass
from html import escape
from pathlib import Path

from flask import Flask, abort, send_file
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


class MobileFileShareServer:
    def __init__(self, paths: list[Path], port: int = DEFAULT_SHARE_PORT + 1, host: str = "0.0.0.0") -> None:
        self.paths = [path.resolve() for path in paths]
        self.port = port
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None
        self._files: dict[str, Path] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._files = _share_files(self.paths)
        if not self._files:
            raise FileNotFoundError("No files to share.")

        app = Flask(__name__)

        @app.get("/")
        def index():
            items = "\n".join(
                f"""<a class="file" href="/download/{escape(file_id)}">
  <span>{escape(path.name)}</span>
  <small>{escape(_format_file_size(path.stat().st_size))}</small>
</a>"""
                for file_id, path in self._files.items()
            )
            return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalNetFTP 文件下载</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background: #f4f7fb;
      color: #172033;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 28px 18px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
    }}
    p {{
      margin: 0 0 18px;
      color: #5b687a;
    }}
    .file {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      min-height: 54px;
      margin-bottom: 10px;
      padding: 0 14px;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      background: white;
      color: #172033;
      text-decoration: none;
      box-shadow: 0 10px 26px rgba(31, 45, 61, 0.08);
    }}
    .file span {{
      overflow-wrap: anywhere;
      font-weight: 600;
    }}
    .file small {{
      flex: 0 0 auto;
      color: #64748b;
    }}
  </style>
</head>
<body>
  <main>
    <h1>LocalNetFTP</h1>
    <p>点击文件下载</p>
    {items}
  </main>
</body>
</html>"""

        @app.get("/download/<file_id>")
        def download(file_id: str):
            path = self._files.get(file_id)
            if path is None or not path.exists():
                abort(404)
            return send_file(path, as_attachment=True, download_name=path.name)

        self._server = make_server(self.host, self.port, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalNetFTPMobileShare",
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
        return lan_download_urls(self.port, "LocalNetFTP", None)


def _share_files(paths: list[Path]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    counter = 1
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            files[str(counter)] = path
            counter += 1
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    files[str(counter)] = child
                    counter += 1
            continue
        raise ValueError(f"Unsupported share path: {path}")
    return files


def _format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _is_lan_address(address: str) -> bool:
    return not address.startswith("127.") and not address.startswith("169.254.")


def _windows_ipconfig_interfaces() -> list[tuple[str, str]]:
    try:
        output = subprocess.check_output(
            ["ipconfig"],
            text=True,
            encoding="gbk",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
            current_name = _normalize_interface_name(line.rstrip(":"))
            continue
        if "IPv4" not in line:
            continue
        _, _, value = line.partition(":")
        address = value.strip()
        if address and _is_lan_address(address):
            interfaces.append((current_name or "局域网", address))

    return interfaces


def _normalize_interface_name(name: str) -> str:
    ethernet_prefix = "Ethernet adapter "
    if name.startswith(ethernet_prefix):
        suffix = name[len(ethernet_prefix) :].strip()
        if suffix.startswith("???"):
            suffix = suffix.replace("???", "", 1).strip()
            return f"以太网 {suffix}".strip()
        return f"以太网 {suffix}".strip()

    wireless_prefix = "Wireless LAN adapter "
    if name.startswith(wireless_prefix):
        suffix = name[len(wireless_prefix) :].strip()
        return f"无线网络 {suffix}".strip()

    return name
