from __future__ import annotations

import json
import hashlib
import socket
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


TRANSFER_VERSION = 1
TRANSFER_REQUEST_TYPE = "localnetftp.transfer.request"
TRANSFER_FILE_TYPE = "localnetftp.transfer.file"
TRANSFER_DIR_TYPE = "localnetftp.transfer.dir"
TRANSFER_DONE_TYPE = "localnetftp.transfer.done"
TRANSFER_ACK_TYPE = "localnetftp.transfer.ack"
CHUNK_SIZE = 1024 * 1024
MAX_FRAME_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TransferItem:
    source_path: Path
    relative_path: str
    is_dir: bool
    size: int = 0
    sha256: str = ""

    def to_manifest_data(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "is_dir": self.is_dir,
            "size": self.size,
            "sha256": self.sha256,
        }


def scan_transfer_items(paths: list[Path]) -> list[TransferItem]:
    items: list[TransferItem] = []
    for path in paths:
        source = path.resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        if source.is_file():
            items.append(
                TransferItem(
                    source_path=source,
                    relative_path=_posix_relative(source.name),
                    is_dir=False,
                    size=source.stat().st_size,
                    sha256=sha256_file(source),
                )
            )
            continue

        if source.is_dir():
            items.append(TransferItem(source_path=source, relative_path=_posix_relative(source.name), is_dir=True))
            for child in sorted(source.rglob("*")):
                relative_path = _posix_relative(source.name, child.relative_to(source))
                items.append(
                    TransferItem(
                        source_path=child,
                        relative_path=relative_path,
                        is_dir=child.is_dir(),
                        size=child.stat().st_size if child.is_file() else 0,
                        sha256=sha256_file(child) if child.is_file() else "",
                    )
                )
            continue

        raise ValueError(f"Unsupported transfer path: {source}")
    return items


def safe_destination_path(receive_dir: Path, relative_path: str) -> Path:
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or any(part in ("", ".", "..") for part in pure_path.parts):
        raise ValueError(f"Unsafe transfer path: {relative_path}")

    root = receive_dir.resolve()
    destination = root.joinpath(*pure_path.parts).resolve()
    if destination != root and root not in destination.parents:
        raise ValueError(f"Transfer path escapes receive directory: {relative_path}")
    return destination


def available_destination_path(path: Path, now: datetime | None = None) -> Path:
    if not path.exists():
        return path

    timestamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S%f")[:-3]
    return path.with_name(f"{path.stem}_{timestamp}{path.suffix}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)))
    sock.sendall(data)


def recv_json(sock: socket.socket) -> dict[str, Any]:
    header = _recv_exact(sock, 4)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > MAX_FRAME_SIZE:
        raise ValueError("Invalid transfer frame size.")
    data = _recv_exact(sock, size)
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Transfer frame must be a JSON object.")
    return payload


def send_file_bytes(
    sock: socket.socket,
    path: Path,
    size: int,
    *,
    offset: int = 0,
    on_chunk: Any = None,
) -> None:
    if offset < 0 or offset > size:
        raise ValueError("Transfer offset must be within the file size.")

    sent = offset
    with path.open("rb") as file:
        file.seek(offset)
        while sent < size:
            chunk = file.read(min(CHUNK_SIZE, size - sent))
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            if on_chunk is not None:
                on_chunk(sent)
    if sent != size:
        raise OSError(f"Failed to read complete file: {path}")


def recv_file_bytes(sock: socket.socket, path: Path, size: int, *, offset: int = 0) -> None:
    if offset < 0 or offset > size:
        raise ValueError("Transfer offset must be within the file size.")

    path.parent.mkdir(parents=True, exist_ok=True)
    remaining = size - offset
    with path.open("ab" if offset else "wb") as file:
        while remaining:
            chunk = sock.recv(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise ConnectionError("Connection closed during file transfer.")
            file.write(chunk)
            remaining -= len(chunk)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("Connection closed while reading transfer frame.")
        chunks.extend(chunk)
    return bytes(chunks)


def _posix_relative(first: str, rest: Path | None = None) -> str:
    if rest is None:
        return PurePosixPath(first).as_posix()
    return PurePosixPath(first, *rest.parts).as_posix()
