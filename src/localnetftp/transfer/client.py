from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TransferProgress:
    event: str
    relative_path: str
    item_index: int
    item_count: int
    bytes_sent: int = 0
    total_bytes: int = 0


ProgressCallback = Callable[[TransferProgress], None]


def send_paths(
    address: str,
    port: int,
    paths: list[Path],
    timeout: float = 15.0,
    on_progress: ProgressCallback | None = None,
) -> None:
    items = scan_transfer_items(paths)
    item_count = len(items)

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

        for index, item in enumerate(items, start=1):
            _emit(on_progress, "start", item.relative_path, index, item_count, total_bytes=item.size)
            if item.is_dir:
                send_json(
                    sock,
                    {
                        "type": TRANSFER_DIR_TYPE,
                        "relative_path": item.relative_path,
                    },
                )
                _emit(on_progress, "done", item.relative_path, index, item_count)
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
                _emit(
                    on_progress,
                    "done",
                    item.relative_path,
                    index,
                    item_count,
                    bytes_sent=item.size,
                    total_bytes=item.size,
                )

        send_json(sock, {"type": TRANSFER_DONE_TYPE})


def _expect_ack(sock: socket.socket) -> bool:
    from localnetftp.transfer.protocol import recv_json

    payload = recv_json(sock)
    return payload.get("type") == TRANSFER_ACK_TYPE and payload.get("accepted") is True


def _emit(
    on_progress: ProgressCallback | None,
    event: str,
    relative_path: str,
    item_index: int,
    item_count: int,
    bytes_sent: int = 0,
    total_bytes: int = 0,
) -> None:
    if on_progress is None:
        return
    on_progress(
        TransferProgress(
            event=event,
            relative_path=relative_path,
            item_index=item_index,
            item_count=item_count,
            bytes_sent=bytes_sent,
            total_bytes=total_bytes,
        )
    )
