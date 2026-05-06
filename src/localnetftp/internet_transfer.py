from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from localnetftp.transfer import ReceiveResult, available_destination_path


def _register_iroh_dll_directory() -> None:
    """Pre-load iroh_ffi.dll on Windows before iroh_ffi.py's ctypes.cdll call.

    In Nuitka onefile the extraction dir name is an 8.3 short path (e.g. ONB809~1).
    ctypes.cdll.LoadLibrary() called from iroh_ffi.py with that short path can fail
    to resolve VCRUNTIME140.dll even though it lives in System32, because the short-path
    form confuses Python 3.8+'s LOAD_LIBRARY_SEARCH_DEFAULT_DIRS.
    Loading the DLL ourselves first via the long-path form puts it in Windows' module
    cache; iroh_ffi.py's subsequent LoadLibrary call returns the cached handle.
    """
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    try:
        import ctypes
        import importlib.util

        spec = importlib.util.find_spec("iroh")
        if spec is None or spec.origin is None:
            return

        # Convert 8.3 short path to long path so AddDllDirectory and LoadLibraryEx agree.
        buf = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetLongPathNameW(str(Path(spec.origin).parent), buf, len(buf))
        dll_dir = Path(buf.value) if buf.value else Path(spec.origin).parent

        os.add_dll_directory(str(dll_dir))
        os.add_dll_directory(str(dll_dir.parent))

        # Pre-load using the long path so Windows caches it before iroh_ffi.py loads it.
        dll_path = dll_dir / "iroh_ffi.dll"
        if dll_path.exists():
            ctypes.CDLL(str(dll_path))
    except Exception:
        pass


_register_iroh_dll_directory()


@dataclass(frozen=True)
class InternetTicket:
    ticket: str


@dataclass(frozen=True)
class InternetTransferProgress:
    role: str
    stage: str
    message: str
    bytes_done: int = 0
    bytes_total: int = 0


TicketCallback = Callable[[InternetTicket], None]
ReceiveCallback = Callable[[ReceiveResult], None]
ErrorCallback = Callable[[str], None]
ProgressCallback = Callable[[InternetTransferProgress], None]


class IrohTicketProvider:
    def __init__(
        self,
        paths: list[Path],
        *,
        work_dir: Path,
        on_ticket: TicketCallback,
        on_error: ErrorCallback,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._paths = [path.resolve() for path in paths]
        self._work_dir = work_dir
        self._on_ticket = on_ticket
        self._on_error = on_error
        self._on_progress = on_progress
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="LocalNetFTPIrohProvider", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._on_error(f"{type(exc).__name__}: {exc}")
        finally:
            if self._temp_dir is not None:
                self._temp_dir.cleanup()
                self._temp_dir = None

    async def _serve(self) -> None:
        import iroh

        self._emit_progress("preparing", "正在准备分享文件")
        share_path = self._prepare_share_path()
        node_dir = self._work_dir / "provider"
        node_dir.mkdir(parents=True, exist_ok=True)
        self._emit_progress("node", "正在启动 iroh 节点")
        node = await iroh.Iroh.persistent(str(node_dir))
        try:
            callback = _AddCallback(self._emit_progress)
            self._emit_progress("adding", "正在导入文件")
            await node.blobs().add_from_path(
                str(share_path),
                False,
                iroh.SetTagOption.auto(),
                iroh.WrapOption.wrap(share_path.name),
                callback,
            )
            if callback.hash is None or callback.format is None:
                raise RuntimeError("Iroh did not return a shareable blob hash.")
            self._emit_progress("ticket", "正在生成 ticket")
            ticket = await node.blobs().share(
                callback.hash,
                callback.format,
                iroh.AddrInfoOptions.RELAY_AND_ADDRESSES,
            )
            self._on_ticket(InternetTicket(str(ticket)))
            self._emit_progress("serving", "ticket 已生成，等待对方下载")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            await node.node().shutdown()

    def _prepare_share_path(self) -> Path:
        if not self._paths:
            raise ValueError("No files selected for internet transfer.")
        for path in self._paths:
            if not path.exists():
                raise FileNotFoundError(path)
        if len(self._paths) == 1:
            return self._paths[0]

        self._temp_dir = tempfile.TemporaryDirectory(prefix="localnetftp-iroh-share-")
        staging_dir = Path(self._temp_dir.name) / "LocalNetFTP"
        staging_dir.mkdir()
        for source in self._paths:
            destination = available_destination_path(staging_dir / source.name)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        return staging_dir

    def _emit_progress(
        self,
        stage: str,
        message: str,
        bytes_done: int = 0,
        bytes_total: int = 0,
    ) -> None:
        if self._on_progress is not None:
            self._on_progress(InternetTransferProgress("send", stage, message, bytes_done, bytes_total))


class IrohTicketReceiver:
    def __init__(
        self,
        ticket: str,
        *,
        receive_dir: Path,
        work_dir: Path,
        on_received: ReceiveCallback,
        on_error: ErrorCallback,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._ticket = ticket.strip()
        self._receive_dir = receive_dir
        self._work_dir = work_dir
        self._on_received = on_received
        self._on_error = on_error
        self._on_progress = on_progress
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="LocalNetFTPIrohReceiver", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._receive())
        except Exception as exc:
            self._on_error(f"{type(exc).__name__}: {exc}")

    async def _receive(self) -> None:
        import iroh

        if not self._ticket:
            raise ValueError("Ticket must not be empty.")

        node_dir = self._work_dir / "receiver"
        node_dir.mkdir(parents=True, exist_ok=True)
        export_temp = tempfile.TemporaryDirectory(prefix="localnetftp-iroh-recv-")
        self._emit_progress("node", "正在启动 iroh 节点")
        node = await iroh.Iroh.persistent(str(node_dir))
        try:
            self._emit_progress("ticket", "正在解析 ticket")
            ticket = iroh.BlobTicket(self._ticket)
            self._emit_progress("connecting", "正在连接对方")
            await node.blobs().download(ticket.hash(), ticket.as_download_options(), _DownloadCallback(self._emit_progress))
            self._emit_progress("exporting", "正在保存到接收目录")
            exported_paths = await _export_ticket(node, ticket, Path(export_temp.name))
            saved_paths = _move_exported_paths(exported_paths, self._receive_dir)
            self._emit_progress("done", "接收完成")
            self._on_received(ReceiveResult(saved_paths))
        finally:
            await node.node().shutdown()
            export_temp.cleanup()

    def _emit_progress(
        self,
        stage: str,
        message: str,
        bytes_done: int = 0,
        bytes_total: int = 0,
    ) -> None:
        if self._on_progress is not None:
            self._on_progress(InternetTransferProgress("receive", stage, message, bytes_done, bytes_total))


class _AddCallback:
    def __init__(self, on_progress: ProgressCallback) -> None:
        self.hash = None
        self.format = None
        self._on_progress = on_progress
        self._sizes: dict[int, int] = {}
        self._offsets: dict[int, int] = {}

    async def progress(self, progress) -> None:
        import iroh

        if progress.type() == iroh.AddProgressType.FOUND:
            found = progress.as_found()
            self._sizes[found.id] = found.size
            self._on_progress("adding", f"正在导入 {found.name}", _sum_values(self._offsets), _sum_values(self._sizes))
        if progress.type() == iroh.AddProgressType.PROGRESS:
            item_progress = progress.as_progress()
            self._offsets[item_progress.id] = item_progress.offset
            self._on_progress("adding", "正在导入文件", _sum_values(self._offsets), _sum_values(self._sizes))
        if progress.type() == iroh.AddProgressType.ALL_DONE:
            done = progress.as_all_done()
            self.hash = done.hash
            self.format = done.format
            self._on_progress("adding", "文件导入完成", _sum_values(self._sizes), _sum_values(self._sizes))
        if progress.type() == iroh.AddProgressType.ABORT:
            raise RuntimeError(progress.as_abort().error)


class _DownloadCallback:
    def __init__(self, on_progress: ProgressCallback) -> None:
        self._on_progress = on_progress
        self._sizes: dict[int, int] = {}
        self._offsets: dict[int, int] = {}

    async def progress(self, progress) -> None:
        import iroh

        if progress.type() == iroh.DownloadProgressType.CONNECTED:
            self._on_progress("downloading", "已连接，正在接收")
        if progress.type() == iroh.DownloadProgressType.FOUND:
            found = progress.as_found()
            self._sizes[found.id] = found.size
            self._on_progress("downloading", "正在接收文件", _sum_values(self._offsets), _sum_values(self._sizes))
        if progress.type() == iroh.DownloadProgressType.PROGRESS:
            item_progress = progress.as_progress()
            self._offsets[item_progress.id] = item_progress.offset
            self._on_progress("downloading", "正在接收文件", _sum_values(self._offsets), _sum_values(self._sizes))
        if progress.type() == iroh.DownloadProgressType.ALL_DONE:
            self._on_progress("downloading", "下载完成", _sum_values(self._sizes), _sum_values(self._sizes))
        if progress.type() == iroh.DownloadProgressType.ABORT:
            raise RuntimeError(progress.as_abort().error)


def _sum_values(values: dict[int, int]) -> int:
    return sum(values.values())


async def _export_ticket(node, ticket, export_dir: Path) -> list[Path]:
    import iroh

    export_dir.mkdir(parents=True, exist_ok=True)
    if ticket.format() == iroh.BlobFormat.HASH_SEQ:
        await node.blobs().export(
            ticket.hash(),
            str(export_dir),
            iroh.BlobExportFormat.COLLECTION,
            iroh.BlobExportMode.COPY,
        )
        return list(export_dir.iterdir())

    destination = export_dir / f"{ticket.hash()}.bin"
    await node.blobs().export(
        ticket.hash(),
        str(destination),
        iroh.BlobExportFormat.BLOB,
        iroh.BlobExportMode.COPY,
    )
    return [destination]


def _move_exported_paths(paths: list[Path], receive_dir: Path) -> list[Path]:
    receive_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for source in paths:
        destination = available_destination_path(receive_dir / source.name)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        saved_paths.append(destination)
    return saved_paths
