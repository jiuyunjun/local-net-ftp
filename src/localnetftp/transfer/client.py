from __future__ import annotations

import socket
from pathlib import Path

from localnetftp.transfer.protocol import (
    TRANSFER_ACK_TYPE,
    TRANSFER_DIR_TYPE,
    TRANSFER_DONE_TYPE,
    TRANSFER_FILE_TYPE,
    TRANSFER_REQUEST_TYPE,
    TRANSFER_VERSION,
    scan_transfer_items,
    send_file_bytes,
    send_json,
)


def send_paths(address: str, port: int, paths: list[Path], timeout: float = 15.0) -> None:
    items = scan_transfer_items(paths)

    with socket.create_connection((address, port), timeout=timeout) as sock:
        send_json(
            sock,
            {
                "type": TRANSFER_REQUEST_TYPE,
                "version": TRANSFER_VERSION,
                "items": [item.to_manifest_data() for item in items],
            },
        )
        ack = _expect_ack(sock)
        if not ack:
            raise ConnectionError("Receiver rejected the transfer request.")

        for item in items:
            if item.is_dir:
                send_json(
                    sock,
                    {
                        "type": TRANSFER_DIR_TYPE,
                        "relative_path": item.relative_path,
                    },
                )
            else:
                send_json(
                    sock,
                    {
                        "type": TRANSFER_FILE_TYPE,
                        "relative_path": item.relative_path,
                        "size": item.size,
                    },
                )
                send_file_bytes(sock, item.source_path, item.size)

        send_json(sock, {"type": TRANSFER_DONE_TYPE})


def _expect_ack(sock: socket.socket) -> bool:
    from localnetftp.transfer.protocol import recv_json

    payload = recv_json(sock)
    return payload.get("type") == TRANSFER_ACK_TYPE and payload.get("accepted") is True
