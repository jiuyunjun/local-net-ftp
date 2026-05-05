from __future__ import annotations

import socket
import threading
from pathlib import Path

from flask import Flask, send_file
from werkzeug.serving import make_server


DEFAULT_SHARE_PORT = 49300


def lan_download_urls(port: int, filename: str, addresses: list[str] | None = None) -> list[str]:
    hosts = addresses if addresses is not None else local_ipv4_addresses()
    return [f"http://{host}:{port}/download/{filename}" for host in hosts]


def local_ipv4_addresses() -> list[str]:
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

    return sorted(addresses)


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
            return f'<a href="/download/{self.file_path.name}">{self.file_path.name}</a>'

        @app.get(f"/download/{self.file_path.name}")
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

    def urls(self) -> list[str]:
        return lan_download_urls(self.port, self.file_path.name)


def _is_lan_address(address: str) -> bool:
    return not address.startswith("127.") and not address.startswith("169.254.")
