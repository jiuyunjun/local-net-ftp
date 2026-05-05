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
        if not ack.accepted:
            raise ConnectionError("Receiver rejected the transfer request.")

        for index, item in enumerate(items, start=1):
            offset = ack.offsets.get(item.relative_path, 0)
            _emit(
                on_progress,
                "start",
                item.relative_path,
                index,
                item_count,
                bytes_sent=offset,
                total_bytes=item.size,
            )
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
                        "sha256": item.sha256,
                        "offset": offset,
                    },
                )
                send_file_bytes(
                    sock,
                    item.source_path,
                    item.size,
                    offset=offset,
                    on_chunk=lambda bytes_sent,
                    relative_path=item.relative_path,
                    item_index=index,
                    total_size=item.size: _emit(
                        on_progress,
                        "progress",
                        relative_path,
                        item_index,
                        item_count,
                        bytes_sent=bytes_sent,
                        total_bytes=total_size,
                    ),
                )
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


@dataclass(frozen=True)
class TransferAck:
    accepted: bool
    offsets: dict[str, int]


def _expect_ack(sock: socket.socket) -> TransferAck:
    from localnetftp.transfer.protocol import recv_json

    payload = recv_json(sock)
    if payload.get("type") != TRANSFER_ACK_TYPE or payload.get("accepted") is not True:
        return TransferAck(accepted=False, offsets={})

    raw_files = payload.get("files")
    offsets: dict[str, int] = {}
    if isinstance(raw_files, dict):
        for relative_path, data in raw_files.items():
            if not isinstance(relative_path, str) or not isinstance(data, dict):
                continue
            offset = data.get("offset")
            if isinstance(offset, int) and offset >= 0:
                offsets[relative_path] = offset
    return TransferAck(accepted=True, offsets=offsets)


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
