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
    """On Windows, add iroh's package directory to the DLL search path.

    Python 3.8+ no longer searches PATH/CWD for native DLL dependencies by default.
    When running as a Nuitka onefile exe the extraction path may be an 8.3 short-form
    temp dir (e.g. ON0689~1), which causes LoadLibraryEx to fail resolving
    iroh_ffi.dll's own dependencies (MSVC runtime, etc.).
    os.add_dll_directory() fixes this without requiring any PATH changes.
    """
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    try:
        import importlib.util

        spec = importlib.util.find_spec("iroh")
        if spec is None or spec.origin is None:
            return
        dll_dir = Path(spec.origin).parent.resolve()
        os.add_dll_directory(str(dll_dir))
        # Nuitka onefile extracts all DLLs to the parent of the package dir;
        # iroh_ffi.dll's own dependencies (MSVC runtime etc.) live there.
        os.add_dll_directory(str(dll_dir.parent))
    except Exception:
        pass


_register_iroh_dll_directory()


@dataclass(frozen=True)
class InternetTicket:
    ticket: str


TicketCallback = Callable[[InternetTicket], None]
ReceiveCallback = Callable[[ReceiveResult], None]
ErrorCallback = Callable[[str], None]


class IrohTicketProvider:
    def __init__(
        self,
        paths: list[Path],
        *,
        work_dir: Path,
        on_ticket: TicketCallback,
        on_error: ErrorCallback,
    ) -> None:
        self._paths = [path.resolve() for path in paths]
        self._work_dir = work_dir
        self._on_ticket = on_ticket
        self._on_error = on_error
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

        share_path = self._prepare_share_path()
        node_dir = self._work_dir / "provider"
        node_dir.mkdir(parents=True, exist_ok=True)
        node = await iroh.Iroh.persistent(str(node_dir))
        try:
            callback = _AddCallback()
            await node.blobs().add_from_path(
                str(share_path),
                False,
                iroh.SetTagOption.auto(),
                iroh.WrapOption.wrap(share_path.name),
                callback,
            )
            if callback.hash is None or callback.format is None:
                raise RuntimeError("Iroh did not return a shareable blob hash.")
            ticket = await node.blobs().share(
                callback.hash,
                callback.format,
                iroh.AddrInfoOptions.RELAY_AND_ADDRESSES,
            )
            self._on_ticket(InternetTicket(str(ticket)))
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


class IrohTicketReceiver:
    def __init__(
        self,
        ticket: str,
        *,
        receive_dir: Path,
        work_dir: Path,
        on_received: ReceiveCallback,
        on_error: ErrorCallback,
    ) -> None:
        self._ticket = ticket.strip()
        self._receive_dir = receive_dir
        self._work_dir = work_dir
        self._on_received = on_received
        self._on_error = on_error
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
        node = await iroh.Iroh.persistent(str(node_dir))
        try:
            ticket = iroh.BlobTicket(self._ticket)
            await node.blobs().download(ticket.hash(), ticket.as_download_options(), _DownloadCallback())
            exported_paths = await _export_ticket(node, ticket, Path(export_temp.name))
            saved_paths = _move_exported_paths(exported_paths, self._receive_dir)
            self._on_received(ReceiveResult(saved_paths))
        finally:
            await node.node().shutdown()
            export_temp.cleanup()


class _AddCallback:
    def __init__(self) -> None:
        self.hash = None
        self.format = None

    async def progress(self, progress) -> None:
        import iroh

        if progress.type() == iroh.AddProgressType.ALL_DONE:
            done = progress.as_all_done()
            self.hash = done.hash
            self.format = done.format
        if progress.type() == iroh.AddProgressType.ABORT:
            raise RuntimeError(progress.as_abort().error)


class _DownloadCallback:
    async def progress(self, progress) -> None:
        import iroh

        if progress.type() == iroh.DownloadProgressType.ABORT:
            raise RuntimeError(progress.as_abort().error)


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
