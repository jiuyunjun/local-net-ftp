from __future__ import annotations

import socket
import threading
from pathlib import Path

from localnetftp.transfer.protocol import (
    TRANSFER_ACK_TYPE,
    TRANSFER_DIR_TYPE,
    TRANSFER_DONE_TYPE,
    TRANSFER_FILE_TYPE,
    TRANSFER_REQUEST_TYPE,
    TRANSFER_VERSION,
    recv_file_bytes,
    recv_json,
    available_destination_path,
    safe_destination_path,
    send_json,
)


class TransferServer:
    def __init__(self, receive_dir: Path, port: int, host: str = "0.0.0.0") -> None:
        self._receive_dir = receive_dir
        self._host = host
        self._port = port
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(0.25)
        self._socket.bind((self._host, self._port))
        self._socket.listen()
        self._thread = threading.Thread(target=self._run, name="LocalNetFTPTransferServer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        server_socket = self._socket
        assert server_socket is not None
        while not self._stop_event.is_set():
            try:
                client, _ = server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(
                target=self._handle_client,
                args=(client,),
                name="LocalNetFTPTransferClient",
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        with client:
            request = recv_json(client)
            if request.get("type") != TRANSFER_REQUEST_TYPE or request.get("version") != TRANSFER_VERSION:
                send_json(client, {"type": TRANSFER_ACK_TYPE, "accepted": False})
                return

            self._validate_manifest(request.get("items"))
            send_json(client, {"type": TRANSFER_ACK_TYPE, "accepted": True})

            while True:
                frame = recv_json(client)
                frame_type = frame.get("type")
                if frame_type == TRANSFER_DONE_TYPE:
                    return
                if frame_type == TRANSFER_DIR_TYPE:
                    destination = safe_destination_path(self._receive_dir, _relative_path(frame))
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if frame_type == TRANSFER_FILE_TYPE:
                    size = frame.get("size")
                    if not isinstance(size, int) or size < 0:
                        raise ValueError("Transfer file size must be a non-negative integer.")
                    destination = available_destination_path(
                        safe_destination_path(self._receive_dir, _relative_path(frame))
                    )
                    recv_file_bytes(client, destination, size)
                    continue
                raise ValueError("Unsupported transfer frame type.")

    def _validate_manifest(self, items: object) -> None:
        if not isinstance(items, list):
            raise ValueError("Transfer request must contain an item list.")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Transfer manifest item must be an object.")
            safe_destination_path(self._receive_dir, _relative_path(item))


def _relative_path(frame: dict) -> str:
    value = frame.get("relative_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Transfer frame must contain a relative path.")
    return value
