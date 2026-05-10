from __future__ import annotations

import socket
import threading
import uuid
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
    recv_file_bytes,
    recv_json,
    available_destination_path,
    safe_destination_path,
    sha256_file,
    send_json,
)


PART_SUFFIX = ".localnetftp.part"
PART_META_SUFFIX = ".localnetftp.part.json"


@dataclass(frozen=True)
class ReceivePlan:
    relative_path: str
    destination: Path
    partial_path: Path
    meta_path: Path
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True)
class ReceiveResult:
    paths: list[Path]
    transfer_id: str = ""


@dataclass(frozen=True)
class ReceiveProgress:
    transfer_id: str
    event: str
    relative_path: str
    item_index: int
    item_count: int
    bytes_done: int = 0
    total_bytes: int = 0
    paths: list[Path] | None = None


ReceiveCallback = Callable[[ReceiveResult], None]
ReceiveProgressCallback = Callable[[ReceiveProgress], None]


class TransferServer:
    def __init__(
        self,
        receive_dir: Path,
        port: int,
        host: str = "0.0.0.0",
        on_received: ReceiveCallback | None = None,
        on_progress: ReceiveProgressCallback | None = None,
    ) -> None:
        self._receive_dir = receive_dir
        self._host = host
        self._port = port
        self._on_received = on_received
        self._on_progress = on_progress
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
        self._port = self._socket.getsockname()[1]
        self._socket.listen()
        self._thread = threading.Thread(target=self._run, name="LocalNetFTPTransferServer", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._port

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
                target=self._handle_client_safely,
                args=(client,),
                name="LocalNetFTPTransferClient",
                daemon=True,
            ).start()

    def _handle_client_safely(self, client: socket.socket) -> None:
        try:
            self._handle_client(client)
        except (ConnectionError, OSError, ValueError):
            return

    def _handle_client(self, client: socket.socket) -> None:
        transfer_id = uuid.uuid4().hex
        with client:
            request = recv_json(client)
            if request.get("type") != TRANSFER_REQUEST_TYPE or request.get("version") != TRANSFER_VERSION:
                send_json(client, {"type": TRANSFER_ACK_TYPE, "accepted": False})
                return

            plans, root_paths = self._prepare_receive_plans(request.get("items"))
            item_order = _manifest_item_order(request.get("items"))
            item_count = len(item_order)
            item_index_by_path = {relative_path: index for index, relative_path in enumerate(item_order, start=1)}
            total_bytes = sum(plan.size for plan in plans.values())
            completed_bytes = 0
            send_json(
                client,
                {
                    "type": TRANSFER_ACK_TYPE,
                    "accepted": True,
                    "files": {
                        relative_path: {"offset": plan.offset}
                        for relative_path, plan in plans.items()
                    },
                },
            )

            while True:
                frame = recv_json(client)
                frame_type = frame.get("type")
                if frame_type == TRANSFER_DONE_TYPE:
                    self._emit_progress(
                        ReceiveProgress(
                            transfer_id=transfer_id,
                            event="all_done",
                            relative_path="",
                            item_index=item_count,
                            item_count=item_count,
                            bytes_done=total_bytes,
                            total_bytes=total_bytes,
                            paths=root_paths,
                        )
                    )
                    self._notify_received(root_paths, transfer_id)
                    return
                if frame_type == TRANSFER_DIR_TYPE:
                    relative_path = _relative_path(frame)
                    destination = safe_destination_path(self._receive_dir, relative_path)
                    index = item_index_by_path.get(relative_path, 0)
                    self._emit_progress(
                        ReceiveProgress(
                            transfer_id=transfer_id,
                            event="start",
                            relative_path=relative_path,
                            item_index=index,
                            item_count=item_count,
                            bytes_done=completed_bytes,
                            total_bytes=total_bytes,
                        )
                    )
                    destination.mkdir(parents=True, exist_ok=True)
                    self._emit_progress(
                        ReceiveProgress(
                            transfer_id=transfer_id,
                            event="done",
                            relative_path=relative_path,
                            item_index=index,
                            item_count=item_count,
                            bytes_done=completed_bytes,
                            total_bytes=total_bytes,
                        )
                    )
                    continue
                if frame_type == TRANSFER_FILE_TYPE:
                    size = frame.get("size")
                    if not isinstance(size, int) or size < 0:
                        raise ValueError("Transfer file size must be a non-negative integer.")
                    relative_path = _relative_path(frame)
                    plan = plans.get(relative_path)
                    if plan is None:
                        raise ValueError("Transfer file was not declared in the manifest.")
                    offset = frame.get("offset", 0)
                    if not isinstance(offset, int) or offset != plan.offset:
                        raise ValueError("Transfer file offset does not match receiver state.")
                    index = item_index_by_path.get(relative_path, 0)
                    completed_before = completed_bytes
                    self._emit_progress(
                        ReceiveProgress(
                            transfer_id=transfer_id,
                            event="start",
                            relative_path=relative_path,
                            item_index=index,
                            item_count=item_count,
                            bytes_done=completed_before + plan.offset,
                            total_bytes=total_bytes,
                        )
                    )
                    recv_file_bytes(
                        client,
                        plan.partial_path,
                        plan.size,
                        offset=plan.offset,
                        on_chunk=lambda bytes_done,
                        relative_path=relative_path,
                        index=index,
                        completed_before=completed_before: self._emit_progress(
                            ReceiveProgress(
                                transfer_id=transfer_id,
                                event="progress",
                                relative_path=relative_path,
                                item_index=index,
                                item_count=item_count,
                                bytes_done=completed_before + bytes_done,
                                total_bytes=total_bytes,
                            )
                        ),
                    )
                    _finalize_plan(plan)
                    completed_bytes = completed_before + plan.size
                    self._emit_progress(
                        ReceiveProgress(
                            transfer_id=transfer_id,
                            event="done",
                            relative_path=relative_path,
                            item_index=index,
                            item_count=item_count,
                            bytes_done=completed_bytes,
                            total_bytes=total_bytes,
                        )
                    )
                    continue
                raise ValueError("Unsupported transfer frame type.")

    def _prepare_receive_plans(self, items: object) -> tuple[dict[str, ReceivePlan], list[Path]]:
        if not isinstance(items, list):
            raise ValueError("Transfer request must contain an item list.")
        plans: dict[str, ReceivePlan] = {}
        root_paths_by_name: dict[str, Path] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Transfer manifest item must be an object.")
            relative_path = _relative_path(item)
            safe_destination_path(self._receive_dir, relative_path)
            root_name = _root_name(relative_path)
            if item.get("is_dir") is True:
                root_paths_by_name.setdefault(root_name, safe_destination_path(self._receive_dir, root_name))
                continue
            plan = _prepare_file_plan(self._receive_dir, item)
            plans[relative_path] = plan
            root_paths_by_name.setdefault(root_name, _root_destination(plan.destination, relative_path))
        return plans, list(root_paths_by_name.values())

    def _notify_received(self, paths: list[Path], transfer_id: str = "") -> None:
        if self._on_received is None or not paths:
            return
        existing_paths = [path for path in paths if path.exists()]
        if existing_paths:
            self._on_received(ReceiveResult(existing_paths, transfer_id=transfer_id))

    def _emit_progress(self, progress: ReceiveProgress) -> None:
        if self._on_progress is not None:
            self._on_progress(progress)


def _relative_path(frame: dict) -> str:
    value = frame.get("relative_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Transfer frame must contain a relative path.")
    return value


def _manifest_item_order(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("relative_path")
            if isinstance(value, str):
                result.append(value)
    return result


def _root_name(relative_path: str) -> str:
    return relative_path.split("/", 1)[0]


def _root_destination(destination: Path, relative_path: str) -> Path:
    depth = len(relative_path.split("/"))
    if depth == 1:
        return destination
    return destination.parents[depth - 2]


def _prepare_file_plan(receive_dir: Path, item: dict) -> ReceivePlan:
    relative_path = _relative_path(item)
    size = item.get("size")
    if not isinstance(size, int) or size < 0:
        raise ValueError("Transfer file size must be a non-negative integer.")
    checksum = item.get("sha256")
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError("Transfer file checksum must be a sha256 hex digest.")

    existing_plan = _find_resumable_plan(receive_dir, relative_path, size, checksum)
    if existing_plan is not None:
        return existing_plan

    destination = available_destination_path(safe_destination_path(receive_dir, relative_path))
    partial_path = _partial_path_for(destination)
    meta_path = _meta_path_for(partial_path)
    _write_plan_meta(receive_dir, meta_path, relative_path, destination, partial_path, size, checksum)
    return ReceivePlan(
        relative_path=relative_path,
        destination=destination,
        partial_path=partial_path,
        meta_path=meta_path,
        offset=0,
        size=size,
        sha256=checksum,
    )


def _find_resumable_plan(receive_dir: Path, relative_path: str, size: int, checksum: str) -> ReceivePlan | None:
    for meta_path in receive_dir.rglob(f"*{PART_META_SUFFIX}"):
        meta = _read_plan_meta(meta_path)
        if (
            meta.get("relative_path") != relative_path
            or meta.get("size") != size
            or meta.get("sha256") != checksum
        ):
            continue

        destination = safe_destination_path(receive_dir, str(meta.get("destination_relative_path", relative_path)))
        partial_relative_path = meta.get("partial_path")
        if not isinstance(partial_relative_path, str):
            continue
        try:
            partial_path = safe_destination_path(receive_dir, partial_relative_path)
        except ValueError:
            continue
        if not partial_path.exists():
            continue
        offset = partial_path.stat().st_size
        if offset > size:
            continue
        if offset == size and sha256_file(partial_path) != checksum:
            continue
        return ReceivePlan(
            relative_path=relative_path,
            destination=destination,
            partial_path=partial_path,
            meta_path=meta_path,
            offset=offset,
            size=size,
            sha256=checksum,
        )
    return None


def _finalize_plan(plan: ReceivePlan) -> None:
    if plan.partial_path.stat().st_size != plan.size:
        raise ValueError("Received file size does not match manifest.")
    if sha256_file(plan.partial_path) != plan.sha256:
        raise ValueError("Received file checksum does not match manifest.")
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    plan.partial_path.replace(plan.destination)
    if plan.meta_path.exists():
        plan.meta_path.unlink()


def _partial_path_for(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}{PART_SUFFIX}")


def _meta_path_for(partial_path: Path) -> Path:
    return partial_path.with_name(f"{partial_path.name}.json")


def _write_plan_meta(
    receive_dir: Path,
    meta_path: Path,
    relative_path: str,
    destination: Path,
    partial_path: Path,
    size: int,
    checksum: str,
) -> None:
    from json import dumps

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "relative_path": relative_path,
        "destination_relative_path": destination.relative_to(receive_dir).as_posix(),
        "partial_path": partial_path.relative_to(receive_dir).as_posix(),
        "size": size,
        "sha256": checksum,
    }
    meta_path.write_text(dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _read_plan_meta(meta_path: Path) -> dict:
    from json import loads

    try:
        payload = loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
