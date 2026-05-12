from __future__ import annotations

import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import subprocess

from localnetftp.config import (
    AppConfig,
    CONFIG_FILE_NAME,
    default_config_dir,
    is_start_on_boot_enabled,
    load_config,
    save_config,
    set_start_on_boot,
)
from localnetftp.internet_transfer import (
    IrohTicketProvider,
    IrohTicketReceiver,
    InternetTicket,
    InternetTransferProgress,
)
from localnetftp.network import DiscoveryService, LocalPeerRegistry, Peer, create_device_identity
from localnetftp.share import (
    DownloadShareServer,
    MobileFileShareServer,
    MobileReceiveServer,
    ShareAddress,
    local_ipv4_interfaces,
)
from localnetftp.transfer import ReceiveProgress, ReceiveResult, TransferProgress, TransferServer, send_paths
from localnetftp.ui.clipboard_payload import timestamped_clipboard_path
from localnetftp.ui.drop_paths import local_paths_from_urls
from localnetftp.ui.send_state import can_send, confirmation_text, send_summary


TRANSFER_LISTEN_PORT = 49200
DEV_TRANSFER_BASE_PORT = 49210


@dataclass(frozen=True)
class RuntimeOptions:
    config_path: Path | None = None
    config_dir: Path | None = None
    transfer_port: int = TRANSFER_LISTEN_PORT
    dev_instance: str = ""
    dev_registry_path: Path | None = None


def dev_runtime_options(instance_name: str) -> RuntimeOptions:
    normalized = instance_name.strip() or "A"
    config_dir = default_config_dir() / "dev" / normalized
    return RuntimeOptions(
        config_path=config_dir / CONFIG_FILE_NAME,
        config_dir=config_dir,
        transfer_port=_dev_transfer_port(normalized),
        dev_instance=normalized,
        dev_registry_path=default_config_dir() / "dev" / "peers.json",
    )


def _dev_transfer_port(instance_name: str) -> int:
    normalized = instance_name.strip().upper()
    if len(normalized) == 1 and "A" <= normalized <= "Z":
        return DEV_TRANSFER_BASE_PORT + ord(normalized) - ord("A")
    return DEV_TRANSFER_BASE_PORT + sum(ord(char) for char in normalized) % 1000


def _dev_related_port(base_port: int, instance_name: str, offset: int = 0) -> int:
    normalized = instance_name.strip().upper()
    if not normalized:
        return base_port
    if len(normalized) == 1 and "A" <= normalized <= "Z":
        return DEV_TRANSFER_BASE_PORT + 100 + (ord(normalized) - ord("A")) * 10 + offset
    return DEV_TRANSFER_BASE_PORT + 100 + (sum(ord(char) for char in normalized) % 100) * 10 + offset


def run_tray_app(options: RuntimeOptions | None = None) -> int:
    from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt, Signal
    from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMenu,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QPlainTextEdit,
        QProgressBar,
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    SHARE_PORT = 49300
    MOBILE_SHARE_PORT = 49301
    MOBILE_RECEIVE_PORT = 49302
    MOBILE_SHARE_NAME = "局域网内手机用户"
    runtime_options = options or RuntimeOptions()

    class NameEdit(QLineEdit):
        def __init__(self, text: str) -> None:
            super().__init__(text)
            self.setReadOnly(True)
            self.setObjectName("floatingName")
            self.setPlaceholderText("我的名字")

        def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if self.isReadOnly():
                self.setReadOnly(False)
                self.setFocus()
                self.selectAll()
            super().mousePressEvent(event)

    class PasteInput(QLineEdit):
        def __init__(self, on_paste, on_submit) -> None:
            super().__init__()
            self._on_paste = on_paste
            self._on_submit = on_submit
            self.setObjectName("pasteInput")
            self.setPlaceholderText("输入文字回车 / 粘贴文字图片文件")
            self.returnPressed.connect(self._submit_text)

        def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.matches(QKeySequence.Paste):
                self._on_paste()
                event.accept()
                return
            super().keyPressEvent(event)

        def _submit_text(self) -> None:
            self._on_submit()

    class UiEvents(QObject):
        received = Signal(object)
        receive_progress = Signal(object)
        send_progress = Signal(object, object, str)
        send_finished = Signal(object, str, bool, bool, str)
        error = Signal(str)
        internet_ticket = Signal(object, object)
        internet_progress = Signal(object)
        show_floating = Signal()
        mobile_receive_ready = Signal(object, object, object)
        mobile_receive_error = Signal(object, str)

    def _qr_pixmap(text: str, scale: int) -> QPixmap:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=1)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix) * scale
        image = QImage(size, size, QImage.Format_RGB32)
        image.fill(0xFFFFFFFF)
        for row_index, row in enumerate(matrix):
            for column_index, dark in enumerate(row):
                if not dark:
                    continue
                for y in range(row_index * scale, (row_index + 1) * scale):
                    for x in range(column_index * scale, (column_index + 1) * scale):
                        image.setPixel(x, y, 0xFF172033)
        return QPixmap.fromImage(image)

    class AppRuntime:
        def __init__(self) -> None:
            self.options = runtime_options
            self.config_path = runtime_options.config_path
            self.config_dir = runtime_options.config_dir or default_config_dir()
            self.debug_log_path = self.config_dir / "debug.log"
            self.config = self._load_initial_config()
            self.discovery_service: DiscoveryService | None = None
            self.transfer_server: TransferServer | None = None
            self.share_server: DownloadShareServer | None = None
            self.mobile_share_server: MobileFileShareServer | None = None
            self.mobile_receive_server: MobileReceiveServer | None = None
            self.internet_providers: list[IrohTicketProvider] = []
            self.internet_receivers: list[IrohTicketReceiver] = []
            self.local_peer_registry = (
                LocalPeerRegistry(runtime_options.dev_registry_path)
                if runtime_options.dev_registry_path is not None
                else None
            )
            self.identity = create_device_identity(
                self.config.device_name,
                listen_port=runtime_options.transfer_port,
                device_id=self.config.device_id,
            )
            self.share_port = _dev_related_port(SHARE_PORT, runtime_options.dev_instance, 0)
            self.mobile_share_port = _dev_related_port(MOBILE_SHARE_PORT, runtime_options.dev_instance, 1)
            self.mobile_receive_port = _dev_related_port(MOBILE_RECEIVE_PORT, runtime_options.dev_instance, 2)
            save_config(self.config, self.config_path)
            self.log_debug("runtime initialized")

        def log_debug(self, message: str) -> None:
            _append_debug_log(self.debug_log_path, message)

        def _load_initial_config(self) -> AppConfig:
            if self.options.dev_instance and self.config_path is not None and not self.config_path.exists():
                config = AppConfig(
                    receive_dir=self.config_dir / "Downloads",
                    confirm_before_send=True,
                    device_name=f"LocalNetFTP {self.options.dev_instance}",
                    device_id=f"localnetftp-dev-{self.options.dev_instance}",
                )
                save_config(config, self.config_path)
                return config
            return load_config(self.config_path)

        def start(self) -> str:
            transfer_error = self.start_transfer_server()
            discovery_error = self.start_discovery()
            if transfer_error:
                return transfer_error
            if discovery_error:
                return discovery_error
            threading.Thread(
                target=local_ipv4_interfaces,
                name="LocalNetFTPNetworkInterfacesWarmup",
                daemon=True,
            ).start()
            return ""

        def stop(self) -> None:
            self.stop_discovery()
            self.stop_transfer_server()
            self.stop_share_server()
            self.stop_mobile_share_server()
            self.stop_mobile_receive_server()
            self.stop_internet_providers()

        def save_settings(self, config: AppConfig) -> str:
            save_config(config, self.config_path)
            self.config = config
            self.identity = create_device_identity(
                self.config.device_name,
                listen_port=self.options.transfer_port,
                device_id=self.config.device_id,
            )
            set_start_on_boot(
                config.start_on_boot,
                _current_executable(),
                app_name="LocalNetFTP",
            )
            self.stop()
            return self.start()

        def start_transfer_server(self) -> str:
            self.transfer_server = TransferServer(
                self.config.receive_dir,
                self.options.transfer_port,
                on_received=self.on_received,
                on_progress=lambda progress: ui_events.receive_progress.emit(progress),
            )
            try:
                self.transfer_server.start()
            except OSError as exc:
                self.transfer_server = None
                return f"文件接收服务启动失败：{exc}"
            return ""

        def stop_transfer_server(self) -> None:
            if self.transfer_server is not None:
                self.transfer_server.stop()
                self.transfer_server = None

        def start_discovery(self) -> str:
            self.discovery_service = DiscoveryService(self.identity)
            try:
                self.discovery_service.start()
            except OSError as exc:
                self.discovery_service = None
                return f"局域网发现启动失败：{exc}"
            self.publish_local_peer()
            return ""

        def stop_discovery(self) -> None:
            if self.local_peer_registry is not None:
                self.local_peer_registry.remove(self.config.device_id)
            if self.discovery_service is not None:
                self.discovery_service.stop()
                self.discovery_service = None

        def peers(self) -> list[Peer]:
            peers: list[Peer] = []
            if self.discovery_service is not None:
                peers.extend(self.discovery_service.peers())
            if self.local_peer_registry is not None:
                self.publish_local_peer()
                by_device_id = {peer.identity.device_id: peer for peer in peers}
                for peer in self.local_peer_registry.peers(self.config.device_id):
                    by_device_id[peer.identity.device_id] = peer
                peers = sorted(by_device_id.values(), key=lambda peer: peer.identity.device_name.casefold())
            return peers

        def publish_local_peer(self) -> None:
            if self.local_peer_registry is not None:
                self.local_peer_registry.publish(self.identity)

        def on_received(self, result: ReceiveResult) -> None:
            ui_events.received.emit(result)

        def start_share_server(self) -> DownloadShareServer:
            if self.share_server is None:
                self.share_server = DownloadShareServer(_share_executable_path(), port=self.share_port)
                self.share_server.start()
            return self.share_server

        def stop_share_server(self) -> None:
            if self.share_server is not None:
                self.share_server.stop()
                self.share_server = None

        def start_mobile_share_server(self, paths: list[Path]) -> MobileFileShareServer:
            self.stop_mobile_share_server()
            self.mobile_share_server = MobileFileShareServer(paths, port=self.mobile_share_port)
            self.mobile_share_server.start()
            return self.mobile_share_server

        def stop_mobile_share_server(self) -> None:
            if self.mobile_share_server is not None:
                self.mobile_share_server.stop()
                self.mobile_share_server = None

        def start_mobile_receive_server(self) -> MobileReceiveServer:
            if self.mobile_receive_server is None:
                self.mobile_receive_server = MobileReceiveServer(
                    self.config.receive_dir,
                    port=self.mobile_receive_port,
                    on_received=lambda paths: ui_events.received.emit(ReceiveResult(paths)),
                )
                self.mobile_receive_server.start()
            return self.mobile_receive_server

        def stop_mobile_receive_server(self) -> None:
            if self.mobile_receive_server is not None:
                self.mobile_receive_server.stop()
                self.mobile_receive_server = None

        def stop_mobile_receive_server_async(self) -> None:
            server = self.mobile_receive_server
            self.mobile_receive_server = None
            if server is None:
                return
            threading.Thread(
                target=server.stop,
                name="LocalNetFTPMobileReceiveStop",
                daemon=True,
            ).start()

        def start_internet_provider(self, paths: list[Path]) -> None:
            provider = IrohTicketProvider(
                paths,
                work_dir=self.config_dir / "iroh",
                on_ticket=lambda ticket: ui_events.internet_ticket.emit(provider, ticket),
                on_error=lambda error: ui_events.error.emit(f"Iroh 发送失败：{error}"),
                on_progress=lambda progress: ui_events.internet_progress.emit(progress),
            )
            self.internet_providers.append(provider)
            provider.start()

        def stop_internet_provider(self, provider: IrohTicketProvider) -> None:
            provider.stop()
            if provider in self.internet_providers:
                self.internet_providers.remove(provider)

        def stop_internet_providers(self) -> None:
            for provider in list(self.internet_providers):
                provider.stop()
            self.internet_providers.clear()

        def receive_internet_ticket(self, ticket: str) -> None:
            receiver = IrohTicketReceiver(
                ticket,
                receive_dir=self.config.receive_dir,
                work_dir=self.config_dir / "iroh",
                on_received=lambda result: ui_events.received.emit(result),
                on_error=lambda error: ui_events.error.emit(f"Iroh 接收失败：{error}"),
                on_progress=lambda progress: ui_events.internet_progress.emit(progress),
            )
            self.internet_receivers.append(receiver)
            receiver.start()

    class FloatingWindow(QWidget):
        def __init__(self, runtime: AppRuntime) -> None:
            super().__init__()
            self._runtime = runtime
            self._peers_by_row = {}
            self._mobile_row: int | None = None
            self._internet_row: int | None = None

            self.setWindowTitle("LocalNetFTP")
            self.setMinimumSize(260, 220)
            self.setMaximumWidth(320)
            self.setAcceptDrops(True)
            self.setWindowOpacity(0.92)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setWindowFlag(Qt.Tool, True)
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self._initial_position_applied = False
            self._drag_offset: QPoint | None = None

            self.device_name = NameEdit(self._runtime.config.device_name)
            self.device_name.editingFinished.connect(self._save_device_name)
            self.device_name.installEventFilter(self)
            close_button = QPushButton("×")
            close_button.setObjectName("floatingClose")
            close_button.setToolTip("关闭")
            close_button.clicked.connect(self.hide)

            title_layout = QHBoxLayout()
            title_layout.setContentsMargins(0, 0, 4, 0)
            title_layout.setSpacing(4)
            title_layout.addWidget(self.device_name, 1)
            title_layout.addWidget(close_button)

            self.peer_list = QListWidget()
            self.peer_list.setAlternatingRowColors(True)
            self.peer_list.setSelectionMode(QListWidget.ExtendedSelection)
            self.peer_list.setToolTip("同一局域网内运行 LocalNetFTP 的电脑会显示在这里")
            self.peer_list.itemSelectionChanged.connect(self._update_send_button)

            self.paste_input = PasteInput(self._paste_clipboard, self._send_typed_text)
            self.transfer_status = QLabel("")
            self.transfer_status.setObjectName("transferStatus")
            self.transfer_progress = QProgressBar()
            self.transfer_progress.setObjectName("transferProgress")
            self.transfer_progress.setRange(0, 100)
            self.transfer_progress.setValue(0)
            self.transfer_progress.hide()

            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)
            layout.addLayout(title_layout)
            layout.addWidget(self.peer_list, 1)
            layout.addWidget(self.paste_input)
            layout.addWidget(self.transfer_status)
            layout.addWidget(self.transfer_progress)

            self.setStyleSheet(_floating_stylesheet())

            self._peer_refresh_timer = QTimer(self)
            self._peer_refresh_timer.setInterval(1000)
            self._peer_refresh_timer.timeout.connect(self._refresh_peers)
            self._peer_refresh_timer.start()

        def set_status(self, text: str) -> None:
            if text:
                print(f"LocalNetFTP: {text}", file=sys.stderr)

        def reload_device_name(self) -> None:
            self.device_name.blockSignals(True)
            self.device_name.setText(self._runtime.config.device_name)
            self.device_name.setReadOnly(True)
            self.device_name.blockSignals(False)

        def apply_initial_position(self, app: QApplication) -> None:
            if self._initial_position_applied:
                return

            screen = app.primaryScreen()
            if screen is None:
                self._initial_position_applied = True
                return

            geometry = screen.availableGeometry()
            self.adjustSize()
            margin = 14
            x = geometry.right() - self.width() - margin
            y = geometry.bottom() - self.height() - margin
            self.move(max(geometry.left(), x), max(geometry.top(), y))
            self._initial_position_applied = True

        def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event) -> None:  # noqa: N802 - Qt method name
            paths = local_paths_from_urls(event.mimeData().urls())
            if paths:
                self._confirm_and_send(paths)
                event.acceptProposedAction()
            else:
                event.ignore()

        def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.button() == Qt.LeftButton and self._can_start_drag(event.position().toPoint()):
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.buttons() & Qt.LeftButton and self._drag_offset is not None:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._drag_offset = None
            super().mouseReleaseEvent(event)

        def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt method name
            if watched is self.device_name:
                if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                    self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    return False
                if event.type() == QEvent.MouseMove and event.buttons() & Qt.LeftButton and self._drag_offset is not None:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    return True
                if event.type() == QEvent.MouseButtonRelease:
                    self._drag_offset = None
                    return False
            return super().eventFilter(watched, event)

        def _can_start_drag(self, position) -> bool:
            child = self.childAt(position)
            return child is None or child in (self.device_name, self.transfer_status)

        def _save_device_name(self) -> None:
            device_name = self.device_name.text().strip()
            if not device_name:
                self.reload_device_name()
                return
            self.device_name.setReadOnly(True)
            if device_name == self._runtime.config.device_name:
                return

            config = AppConfig(
                receive_dir=self._runtime.config.receive_dir,
                start_on_boot=self._runtime.config.start_on_boot,
                confirm_before_send=self._runtime.config.confirm_before_send,
                device_name=device_name,
                device_id=self._runtime.config.device_id,
            )
            self.set_status(self._runtime.save_settings(config))

        def _paste_clipboard(self) -> None:
            paths = self._paths_from_clipboard()
            if paths:
                self.paste_input.clear()
                self._confirm_and_send(paths)

        def _send_typed_text(self) -> None:
            text = self.paste_input.text()
            if not text.strip():
                return
            clipboard_dir = self._runtime.config_dir / "clipboard"
            clipboard_dir.mkdir(parents=True, exist_ok=True)
            path = timestamped_clipboard_path(clipboard_dir, ".txt")
            path.write_text(text, encoding="utf-8")
            self.paste_input.clear()
            self._confirm_and_send([path])

        def _paths_from_clipboard(self) -> list[Path]:
            clipboard = QApplication.clipboard()
            mime_data = clipboard.mimeData()

            if mime_data.hasUrls():
                return local_paths_from_urls(mime_data.urls())

            clipboard_dir = self._runtime.config_dir / "clipboard"
            clipboard_dir.mkdir(parents=True, exist_ok=True)

            if mime_data.hasImage():
                path = timestamped_clipboard_path(clipboard_dir, ".png")
                image = clipboard.image()
                image.save(str(path), "PNG")
                return [path]

            if mime_data.hasText():
                text = mime_data.text()
                if text:
                    path = timestamped_clipboard_path(clipboard_dir, ".txt")
                    path.write_text(text, encoding="utf-8")
                    return [path]

            return []

        def _selected_peer_names(self) -> list[str]:
            names = [peer.identity.device_name for peer in self._selected_peers()]
            if self._is_mobile_selected():
                names.append(MOBILE_SHARE_NAME)
            if self._is_internet_selected():
                names.append("局域网外用户")
            return names

        def _selected_peers(self):
            peers = []
            for item in self.peer_list.selectedItems():
                row = self.peer_list.row(item)
                peer = self._peers_by_row.get(row)
                if peer is not None:
                    peers.append(peer)
            return peers

        def _is_internet_selected(self) -> bool:
            if self._internet_row is None:
                return False
            return any(self.peer_list.row(item) == self._internet_row for item in self.peer_list.selectedItems())

        def _is_mobile_selected(self) -> bool:
            if self._mobile_row is None:
                return False
            return any(self.peer_list.row(item) == self._mobile_row for item in self.peer_list.selectedItems())

        def _update_send_button(self) -> None:
            return

        def _confirm_and_send(self, paths: list[Path]) -> None:
            peers = self._selected_peers()
            peer_names = [peer.identity.device_name for peer in peers]
            mobile_selected = self._is_mobile_selected()
            internet_selected = self._is_internet_selected()
            display_names = list(peer_names)
            if mobile_selected:
                display_names.append(MOBILE_SHARE_NAME)
            if internet_selected:
                display_names.append("局域网外用户")
            if not can_send(len(display_names), len(paths)):
                QMessageBox.information(self, "LocalNetFTP", "请先选择要发送给谁。")
                self._update_send_button()
                return

            if self._runtime.config.confirm_before_send:
                result = QMessageBox.question(
                    self,
                    "确认发送",
                    confirmation_text(display_names, paths),
                    QMessageBox.Ok | QMessageBox.Cancel,
                    QMessageBox.Ok,
                )
                if result != QMessageBox.Ok:
                    self._update_send_button()
                    return

            if mobile_selected:
                self.transfer_status.setText("正在生成手机网页下载地址...")
                self._show_progress(0)
                show_mobile_share_window(paths)

            if internet_selected:
                self.transfer_status.setText("正在生成公网 ticket...")
                self._show_progress(0)
                self._runtime.start_internet_provider(paths)

            if peers:
                print(f"LocalNetFTP: {send_summary(peer_names, paths)}", file=sys.stderr)
                self._update_send_button()
                for peer in peers:
                    start_send_task(peer, list(paths))

        def _set_lan_progress(self, peer_name: str, progress: TransferProgress) -> None:
            if progress.total_bytes > 0:
                percent = round(progress.bytes_sent * 100 / progress.total_bytes)
                self.transfer_status.setText(
                    f"{peer_name}: {progress.item_index}/{progress.item_count} {progress.relative_path} {percent}%"
                )
                self._show_progress(percent)
                return
            self.transfer_status.setText(
                f"{peer_name}: {progress.item_index}/{progress.item_count} {progress.relative_path}"
            )
            self._show_progress(0)

        def set_internet_progress(self, progress: InternetTransferProgress) -> None:
            if not self.isVisible():
                ui_events.show_floating.emit()
            if progress.bytes_total > 0:
                percent = round(progress.bytes_done * 100 / progress.bytes_total)
                self.transfer_status.setText(f"{progress.message} {percent}%")
                self._set_progress(percent)
                if progress.stage in ("done", "peer_done"):
                    self._finish_progress(progress.message)
                return
            self.transfer_status.setText(progress.message)
            if progress.stage in ("done", "serving", "peer_done"):
                self._finish_progress(progress.message)
            else:
                self._set_progress(0)

        def _show_progress(self, value: int) -> None:
            self._set_progress(value)

        def _set_progress(self, value: int) -> None:
            self.transfer_progress.show()
            self.transfer_progress.setValue(max(0, min(100, value)))

        def _finish_progress(self, message: str) -> None:
            self.transfer_status.setText(message)
            self._set_progress(100)
            QTimer.singleShot(5000, self._reset_progress)

        def _reset_progress(self) -> None:
            self.transfer_progress.hide()
            self.transfer_progress.setValue(0)

        def _refresh_peers(self) -> None:
            selected_names = set(self._selected_peer_names())
            self.peer_list.clear()
            self._peers_by_row = {}
            self._mobile_row = None

            peers = self._runtime.peers()
            for peer in peers:
                row = self.peer_list.count()
                peer_name = peer.identity.device_name
                self._peers_by_row[row] = peer
                self.peer_list.addItem(f"{peer_name}  {peer.address}:{peer.identity.listen_port}")
                if peer_name in selected_names:
                    self.peer_list.item(row).setSelected(True)

            self._mobile_row = self.peer_list.count()
            self.peer_list.addItem("局域网内手机用户（网页）")
            if MOBILE_SHARE_NAME in selected_names:
                self.peer_list.item(self._mobile_row).setSelected(True)

            self._internet_row = self.peer_list.count()
            self.peer_list.addItem("局域网外用户（生成 ticket）")
            if "局域网外用户" in selected_names:
                self.peer_list.item(self._internet_row).setSelected(True)
            self._update_send_button()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            event.ignore()
            self.hide()

    class SettingsWindow(QWidget):
        def __init__(self, runtime: AppRuntime, on_saved) -> None:
            super().__init__()
            self._runtime = runtime
            self._on_saved = on_saved

            self.setWindowTitle("LocalNetFTP 设置")
            self.setMinimumSize(520, 180)

            device_label = QLabel("本机名称")
            self.device_name = QLineEdit()
            self.device_name.setPlaceholderText("例如：客厅电脑")

            receive_label = QLabel("接收目录")
            self.receive_dir = QLineEdit()
            browse_button = QPushButton("浏览")
            browse_button.clicked.connect(self._choose_receive_dir)

            receive_layout = QHBoxLayout()
            receive_layout.addWidget(self.receive_dir, 1)
            receive_layout.addWidget(browse_button)

            self.start_on_boot = QCheckBox("开机自动启动")
            self.confirm_before_send = QCheckBox("发送前显示确认框")

            save_button = QPushButton("保存设置")
            save_button.clicked.connect(self._save)

            layout = QVBoxLayout(self)
            layout.addWidget(device_label)
            layout.addWidget(self.device_name)
            layout.addWidget(receive_label)
            layout.addLayout(receive_layout)
            layout.addWidget(self.start_on_boot)
            layout.addWidget(self.confirm_before_send)
            layout.addStretch(1)
            layout.addWidget(save_button, alignment=Qt.AlignRight)

            self.setStyleSheet(_app_stylesheet())
            self.reload()

        def reload(self) -> None:
            config = self._runtime.config
            self.device_name.setText(config.device_name)
            self.receive_dir.setText(str(config.receive_dir))
            self.start_on_boot.setChecked(
                is_start_on_boot_enabled(_current_executable(), app_name="LocalNetFTP")
            )
            self.confirm_before_send.setChecked(config.confirm_before_send)

        def _choose_receive_dir(self) -> None:
            selected = QFileDialog.getExistingDirectory(self, "选择接收目录", self.receive_dir.text())
            if selected:
                self.receive_dir.setText(selected)

        def _save(self) -> None:
            receive_dir = Path(self.receive_dir.text()).expanduser()
            device_name = self.device_name.text().strip()
            if not device_name:
                QMessageBox.warning(self, "LocalNetFTP", "本机名称不能为空。")
                return

            config = AppConfig(
                receive_dir=receive_dir,
                start_on_boot=self.start_on_boot.isChecked(),
                confirm_before_send=self.confirm_before_send.isChecked(),
                device_name=device_name,
                device_id=self._runtime.config.device_id,
            )
            status = self._runtime.save_settings(config)
            self._on_saved(status)
            QMessageBox.information(self, "LocalNetFTP", "设置已保存。")

    class ShareWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("投送模式")
            self.setMinimumSize(520, 180)

            self.url_list = QListWidget()
            self.url_list.itemClicked.connect(self._copy_item)
            self.copy_status = QLabel("")

            layout = QVBoxLayout(self)
            layout.addWidget(self.url_list)
            layout.addWidget(self.copy_status)
            self.setStyleSheet(_app_stylesheet())

        def set_urls(self, urls: list[ShareAddress]) -> None:
            self.url_list.clear()
            if not urls:
                self.url_list.addItem("未找到可用局域网地址")
                return
            for item in urls:
                self.url_list.addItem(f"{item.interface_name}    {item.url}")

        def _copy_item(self, item) -> None:
            text = item.text()
            if "http://" not in text:
                return
            url = text[text.index("http://") :]
            QApplication.clipboard().setText(url)
            self.copy_status.setText("已复制")

    class MobileShareWindow(QWidget):
        def __init__(self, runtime: AppRuntime, server: MobileFileShareServer) -> None:
            super().__init__()
            self._runtime = runtime
            self._server = server
            self.setWindowTitle("手机网页下载")
            self.setMinimumSize(520, 420)

            title = QLabel("手机网页下载")
            title.setObjectName("titleLabel")
            label = QLabel("窗口存在期间，手机可访问这些网址下载文件。")
            label.setObjectName("mutedLabel")
            self.status = QLabel("扫描二维码，或点击网址复制")
            self.status.setObjectName("statusLabel")

            self.address_layout = QVBoxLayout()
            self.address_layout.setContentsMargins(0, 0, 0, 0)
            self.address_layout.setSpacing(10)
            address_container = QWidget()
            address_container.setLayout(self.address_layout)

            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.NoFrame)
            scroll_area.setWidget(address_container)

            close_button = QPushButton("停止分享")
            close_button.clicked.connect(self.close)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)
            layout.addWidget(title)
            layout.addWidget(label)
            layout.addWidget(scroll_area, 1)
            layout.addWidget(self.status)
            layout.addWidget(close_button, alignment=Qt.AlignRight)
            self.setStyleSheet(_app_stylesheet())
            self._set_urls(server.urls())

        def _set_urls(self, urls: list[ShareAddress]) -> None:
            while self.address_layout.count():
                item = self.address_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            if not urls:
                self.address_layout.addWidget(QLabel("未找到可用局域网地址"))
                return
            for address in urls:
                self.address_layout.addWidget(self._address_widget(address))
            self.address_layout.addStretch(1)

        def _address_widget(self, address: ShareAddress) -> QWidget:
            card = QWidget()
            card.setObjectName("shareAddressCard")

            interface_label = QLabel(address.interface_name)
            interface_label.setObjectName("shareInterfaceLabel")
            url_button = QPushButton(address.url)
            url_button.setObjectName("linkButton")
            url_button.clicked.connect(lambda: self._copy_url(address.url))
            qr_label = QLabel()
            qr_label.setAlignment(Qt.AlignCenter)
            qr_label.setPixmap(_qr_pixmap(address.url, 4))

            text_layout = QVBoxLayout()
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(6)
            text_layout.addWidget(interface_label)
            text_layout.addWidget(url_button)

            row_layout = QHBoxLayout(card)
            row_layout.setContentsMargins(10, 10, 10, 10)
            row_layout.setSpacing(12)
            row_layout.addWidget(qr_label)
            row_layout.addLayout(text_layout, 1)
            return card

        def _copy_url(self, url: str) -> None:
            QApplication.clipboard().setText(url)
            self.status.setText("已复制网址")

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._runtime.stop_mobile_share_server()
            super().closeEvent(event)

    class MobileReceiveWindow(QWidget):
        def __init__(self, runtime: AppRuntime) -> None:
            super().__init__()
            self._runtime = runtime
            self._server: MobileReceiveServer | None = None
            self._closed = False
            self.setWindowTitle("从手机接收")
            self.setMinimumSize(520, 420)
            self.setAttribute(Qt.WA_DeleteOnClose, True)
            self.setWindowFlag(Qt.Window, True)

            title = QLabel("从手机接收")
            title.setObjectName("titleLabel")
            label = QLabel("窗口存在期间，手机可访问这些网址上传文件、图片或文字。")
            label.setObjectName("mutedLabel")
            self.status = QLabel("扫描二维码，或点击网址复制")
            self.status.setObjectName("statusLabel")

            self.address_layout = QVBoxLayout()
            self.address_layout.setContentsMargins(0, 0, 0, 0)
            self.address_layout.setSpacing(10)
            address_container = QWidget()
            address_container.setLayout(self.address_layout)

            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.NoFrame)
            scroll_area.setWidget(address_container)

            close_button = QPushButton("停止接收")
            close_button.clicked.connect(self.close)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)
            layout.addWidget(title)
            layout.addWidget(label)
            layout.addWidget(scroll_area, 1)
            layout.addWidget(self.status)
            layout.addWidget(close_button, alignment=Qt.AlignRight)
            self.setStyleSheet(_app_stylesheet())
            self._set_loading()

        @property
        def is_closed(self) -> bool:
            return self._closed

        def set_ready(self, server: MobileReceiveServer, urls: list[ShareAddress]) -> None:
            self._server = server
            self.status.setText("扫描二维码，或点击网址复制")
            self._set_urls(urls)

        def set_error(self, message: str) -> None:
            self.status.setText(message)
            self._set_urls([])

        def _set_loading(self) -> None:
            self.status.setText("正在初始化...")
            self._clear_addresses()
            loading = QLabel("正在启动手机接收服务...")
            loading.setObjectName("statusLabel")
            self.address_layout.addWidget(loading)
            self.address_layout.addStretch(1)

        def _set_urls(self, urls: list[ShareAddress]) -> None:
            self._clear_addresses()
            if not urls:
                self.address_layout.addWidget(QLabel("未找到可用局域网地址"))
                return
            for address in urls:
                self.address_layout.addWidget(self._address_widget(address))
            self.address_layout.addStretch(1)

        def _clear_addresses(self) -> None:
            while self.address_layout.count():
                item = self.address_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        def _address_widget(self, address: ShareAddress) -> QWidget:
            card = QWidget()
            card.setObjectName("shareAddressCard")

            interface_label = QLabel(address.interface_name)
            interface_label.setObjectName("shareInterfaceLabel")
            url_button = QPushButton(address.url)
            url_button.setObjectName("linkButton")
            url_button.clicked.connect(lambda: self._copy_url(address.url))
            qr_label = QLabel()
            qr_label.setAlignment(Qt.AlignCenter)
            qr_label.setPixmap(_qr_pixmap(address.url, 4))

            text_layout = QVBoxLayout()
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(6)
            text_layout.addWidget(interface_label)
            text_layout.addWidget(url_button)

            row_layout = QHBoxLayout(card)
            row_layout.setContentsMargins(10, 10, 10, 10)
            row_layout.setSpacing(12)
            row_layout.addWidget(qr_label)
            row_layout.addLayout(text_layout, 1)
            return card

        def _copy_url(self, url: str) -> None:
            QApplication.clipboard().setText(url)
            self.status.setText("已复制网址")

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._closed = True
            self._runtime.stop_mobile_receive_server_async()
            super().closeEvent(event)

    class TicketWindow(QWidget):
        def __init__(self, runtime: AppRuntime, provider: IrohTicketProvider, ticket: InternetTicket) -> None:
            super().__init__()
            self._runtime = runtime
            self._provider = provider
            self.setWindowTitle("公网 ticket")
            self.setMinimumSize(560, 300)
            self.setWindowFlag(Qt.Window, True)

            title = QLabel("公网 ticket")
            title.setObjectName("titleLabel")
            label = QLabel("把这个 ticket 发给对方。窗口关闭前，文件会保持可下载。")
            label.setObjectName("mutedLabel")
            self.ticket_text = QPlainTextEdit(ticket.ticket)
            self.ticket_text.setReadOnly(True)
            self.status = QLabel("ticket 已生成，等待对方下载")
            self.status.setObjectName("statusLabel")
            self.progress = QProgressBar()
            self.progress.setObjectName("inlineProgress")
            self.progress.setRange(0, 100)
            self.progress.setValue(100)

            copy_button = QPushButton("复制 ticket")
            copy_button.clicked.connect(self._copy_ticket)
            close_button = QPushButton("停止分享")
            close_button.clicked.connect(self.close)

            button_layout = QHBoxLayout()
            button_layout.addWidget(copy_button)
            button_layout.addWidget(close_button)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)
            layout.addWidget(title)
            layout.addWidget(label)
            layout.addWidget(self.ticket_text, 1)
            layout.addWidget(self.status)
            layout.addWidget(self.progress)
            layout.addLayout(button_layout)
            self.setStyleSheet(_app_stylesheet())

        def set_progress(self, progress: InternetTransferProgress) -> None:
            self.status.setText(_internet_progress_text(progress))
            self.progress.setValue(_internet_progress_percent(progress))

        def _copy_ticket(self) -> None:
            QApplication.clipboard().setText(self.ticket_text.toPlainText())
            self.status.setText("已复制 ticket")

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._runtime.stop_internet_provider(self._provider)
            super().closeEvent(event)

    class TicketInputWindow(QWidget):
        def __init__(self, on_submit) -> None:
            super().__init__()
            self._on_submit = on_submit
            self.setWindowTitle("输入 ticket")
            self.setMinimumSize(520, 260)

            title = QLabel("输入 ticket")
            title.setObjectName("titleLabel")
            label = QLabel("粘贴对方发来的 ticket")
            label.setObjectName("mutedLabel")
            self.ticket_text = QPlainTextEdit()
            self.ticket_text.setPlaceholderText("ticket")
            self.status = QLabel("")
            self.status.setObjectName("statusLabel")

            receive_button = QPushButton("开始接收")
            receive_button.clicked.connect(self._submit)
            close_button = QPushButton("取消")
            close_button.clicked.connect(self.close)

            button_layout = QHBoxLayout()
            button_layout.addStretch(1)
            button_layout.addWidget(receive_button)
            button_layout.addWidget(close_button)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)
            layout.addWidget(title)
            layout.addWidget(label)
            layout.addWidget(self.ticket_text, 1)
            layout.addWidget(self.status)
            layout.addLayout(button_layout)
            self.setStyleSheet(_app_stylesheet())

        def _submit(self) -> None:
            ticket = self.ticket_text.toPlainText().strip()
            if not ticket:
                self.status.setText("请先粘贴 ticket")
                return
            self._on_submit(ticket)
            self.close()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            super().closeEvent(event)

    class ReceiveProgressWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("接收 ticket 文件")
            self.setMinimumSize(420, 120)

            self.status = QLabel("正在准备接收")
            self.progress = QProgressBar()
            self.progress.setObjectName("toastProgress")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)

            layout = QVBoxLayout(self)
            layout.addWidget(self.status)
            layout.addWidget(self.progress)
            self.setStyleSheet(_app_stylesheet())

        def set_progress(self, progress: InternetTransferProgress) -> None:
            self.status.setText(_internet_progress_text(progress))
            self.progress.setValue(_internet_progress_percent(progress))
            if progress.stage == "done":
                QTimer.singleShot(1600, self.close)

    class SendProgressWindow(QWidget):
        def __init__(self, peer_name: str, paths: list[Path], on_cancel, on_retry) -> None:
            super().__init__()
            self.peer_name = peer_name
            self.paths = paths
            self._on_cancel = on_cancel
            self._on_retry = on_retry
            self.setWindowTitle("发送文件")
            self.setWindowFlag(Qt.Tool, True)
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_DeleteOnClose, True)
            self.setFixedWidth(340)
            self._drag_offset: QPoint | None = None
            self._manually_moved = False
            self._finished = False

            self.status = QLabel(f"正在发送给 {peer_name}")
            self.status.setObjectName("toastTitle")
            self.status.setWordWrap(True)
            self.status.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            close_button = QPushButton("×")
            close_button.setObjectName("toastClose")
            close_button.setToolTip("关闭")
            close_button.clicked.connect(self.close)

            title_layout = QHBoxLayout()
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.addWidget(self.status, 1)
            title_layout.addWidget(close_button)

            self.detail = QLabel(_send_items_text(paths))
            self.detail.setObjectName("toastMessage")
            self.detail.setWordWrap(True)
            self.detail.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.progress = QProgressBar()
            self.progress.setObjectName("toastProgress")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.cancel_button = QPushButton("取消")
            self.cancel_button.clicked.connect(self._cancel)
            self.retry_button = QPushButton("重试")
            self.retry_button.clicked.connect(self._retry)
            self.retry_button.hide()

            button_layout = QHBoxLayout()
            button_layout.addStretch(1)
            button_layout.addWidget(self.retry_button)
            button_layout.addWidget(self.cancel_button)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 10, 12, 12)
            layout.setSpacing(8)
            layout.addLayout(title_layout)
            layout.addWidget(self.detail)
            layout.addWidget(self.progress)
            layout.addLayout(button_layout)
            self.setStyleSheet(_toast_stylesheet())

        def set_progress(self, progress: TransferProgress) -> None:
            percent = _transfer_progress_percent(progress.bytes_sent, progress.total_bytes)
            self.status.setText(
                f"正在发送给 {self.peer_name}：{progress.item_index}/{progress.item_count} {progress.relative_path} {percent}%"
            )
            self.progress.setValue(percent)

        def finish_success(self) -> None:
            self._finished = True
            self.status.setText(f"发送给 {self.peer_name} 完成")
            self.progress.setValue(100)
            self.cancel_button.hide()
            self.retry_button.hide()
            QTimer.singleShot(5000, self.close)

        def finish_failed(self, message: str) -> None:
            self._finished = True
            self.status.setText(f"发送给 {self.peer_name} 失败：{message}")
            self.cancel_button.hide()
            self.retry_button.show()

        def finish_cancelled(self) -> None:
            self._finished = True
            self.status.setText(f"已取消发送给 {self.peer_name}")
            self.cancel_button.hide()
            self.retry_button.show()

        def _cancel(self) -> None:
            if self._finished:
                return
            self._on_cancel()
            self.status.setText(f"正在取消发送给 {self.peer_name}...")
            self.cancel_button.setEnabled(False)

        def _retry(self) -> None:
            self.close()
            self._on_retry()

        def show_near_tray(self, app: QApplication, tray_icon: QSystemTrayIcon | None, stack_index: int = 0) -> None:
            if self._manually_moved:
                self.show()
                return
            _show_widget_near_tray(self, app, tray_icon, stack_index)

        def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.button() == Qt.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.buttons() & Qt.LeftButton and self._drag_offset is not None:
                self._manually_moved = True
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._drag_offset = None
            super().mouseReleaseEvent(event)

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if not self._finished:
                self._cancel()
            super().closeEvent(event)

    class ReceiveToast(QWidget):
        def __init__(self, result: ReceiveResult | None = None) -> None:
            super().__init__()
            self.paths = result.paths if result is not None else []
            self._preview_widget_ref: QWidget | None = None
            self.setWindowTitle("收到文件")
            self.setWindowFlag(Qt.Tool, True)
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_DeleteOnClose, True)
            self.setFixedWidth(340)
            self._drag_offset: QPoint | None = None
            self._manually_moved = False

            title = QLabel("收到文件")
            title.setObjectName("toastTitle")
            title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            close_button = QPushButton("×")
            close_button.setObjectName("toastClose")
            close_button.setToolTip("关闭")
            close_button.clicked.connect(self.close)

            title_layout = QHBoxLayout()
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.addWidget(title, 1)
            title_layout.addWidget(close_button)

            self.message = QLabel(_received_message(self.paths) if self.paths else "正在接收文件")
            self.message.setObjectName("toastMessage")
            self.message.setWordWrap(True)
            self.message.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)

            self.open_folder = QPushButton("打开保存位置")
            self.open_folder.clicked.connect(self._open_save_location)
            self.open_item = QPushButton(_open_item_button_text(self.paths) if self.paths else "打开文件")
            self.open_item.clicked.connect(self._open_received_item)
            if not self.paths:
                self.open_folder.hide()
                self.open_item.hide()

            button_layout = QHBoxLayout()
            button_layout.addWidget(self.open_folder)
            button_layout.addWidget(self.open_item)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 10, 12, 12)
            layout.setSpacing(8)
            layout.addLayout(title_layout)
            layout.addWidget(self.message)
            layout.addWidget(self.progress)
            layout.addLayout(button_layout)
            self._layout = layout

            self.setStyleSheet(_toast_stylesheet())
            if self.paths:
                self.complete(ReceiveResult(self.paths))

        def _preview_widget(self):
            if len(self.paths) != 1 or not self.paths[0].is_file():
                return None
            path = self.paths[0]
            if _is_image_preview_path(path):
                pixmap = QPixmap(str(path))
                if pixmap.isNull():
                    return None
                preview = QLabel()
                preview.setObjectName("toastImagePreview")
                preview.setAlignment(Qt.AlignCenter)
                preview.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                preview.setPixmap(pixmap.scaled(316, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return preview
            if _is_text_preview_path(path):
                text = _read_text_preview(path)
                if text is None:
                    return None
                container = QWidget()
                container.setObjectName("toastPreviewContainer")
                text_box = QPlainTextEdit(text)
                text_box.setObjectName("toastTextPreview")
                text_box.setReadOnly(True)
                text_box.setFixedHeight(92)
                copy_button = QPushButton("复制文字")
                copy_button.clicked.connect(lambda: self._copy_preview_text(text_box, copy_button))
                layout = QVBoxLayout(container)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(6)
                layout.addWidget(text_box)
                layout.addWidget(copy_button, alignment=Qt.AlignRight)
                return container
            return None

        def _copy_preview_text(self, text_box: QPlainTextEdit, copy_button: QPushButton) -> None:
            QApplication.clipboard().setText(text_box.toPlainText())
            copy_button.setText("已复制")
            QTimer.singleShot(1600, lambda: copy_button.setText("复制文字"))

        def set_progress(self, progress: ReceiveProgress) -> None:
            percent = _transfer_progress_percent(progress.bytes_done, progress.total_bytes)
            if progress.relative_path:
                self.message.setText(
                    f"正在接收：{progress.item_index}/{progress.item_count} {progress.relative_path} {percent}%"
                )
            else:
                self.message.setText(f"正在接收文件 {percent}%")
            self.progress.setValue(percent)
            QTimer.singleShot(0, position_received_toasts)

        def set_internet_progress(self, progress: InternetTransferProgress) -> None:
            self.message.setText(_internet_progress_text(progress))
            self.progress.setValue(_internet_progress_percent(progress))
            QTimer.singleShot(0, position_received_toasts)

        def complete(self, result: ReceiveResult) -> None:
            self.paths = result.paths
            self.message.setText(_received_message(self.paths))
            self.progress.setValue(100)
            self.progress.hide()
            self.open_folder.show()
            self.open_item.setText(_open_item_button_text(self.paths))
            self.open_item.show()
            if self._preview_widget_ref is not None:
                self._preview_widget_ref.setParent(None)
                self._preview_widget_ref.deleteLater()
            self._preview_widget_ref = self._preview_widget()
            if self._preview_widget_ref is not None:
                self._layout.insertWidget(2, self._preview_widget_ref)
            self.adjustSize()
            QTimer.singleShot(0, position_received_toasts)

        def show_near_tray(self, app: QApplication, tray_icon: QSystemTrayIcon | None, stack_index: int = 0) -> None:
            if self._manually_moved:
                self.show()
                return
            _show_widget_near_tray(self, app, tray_icon, stack_index)

        def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.button() == Qt.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.buttons() & Qt.LeftButton and self._drag_offset is not None:
                self._manually_moved = True
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._drag_offset = None
            super().mouseReleaseEvent(event)

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            remove_received_toast(self)
            super().closeEvent(event)

        def _open_save_location(self) -> None:
            if not self.paths:
                return
            _open_save_location(self.paths)
            self.close()

        def _open_received_item(self) -> None:
            if not self.paths:
                return
            _open_received_item(self.paths)
            self.close()

    active_toasts: list[ReceiveToast] = []
    receive_toasts_by_id: dict[str, ReceiveToast] = {}
    internet_receive_toasts: list[ReceiveToast] = []
    tray: QSystemTrayIcon | None = None

    def show_received_prompt(result: ReceiveResult) -> None:
        paths = result.paths
        if not paths:
            return
        if result.transfer_id:
            toast = receive_toasts_by_id.pop(result.transfer_id, None)
            if toast is not None:
                toast.complete(result)
                position_received_toasts()
                return
        if internet_receive_toasts:
            toast = internet_receive_toasts.pop(0)
            toast.complete(result)
            position_received_toasts()
            return

        toast = ReceiveToast(result)
        active_toasts.append(toast)
        position_received_toasts()

    def show_receive_progress(progress: ReceiveProgress) -> None:
        toast = receive_toasts_by_id.get(progress.transfer_id)
        if toast is None:
            toast = ReceiveToast()
            receive_toasts_by_id[progress.transfer_id] = toast
            active_toasts.append(toast)
        if progress.event == "all_done" and progress.paths is not None:
            toast.complete(ReceiveResult(progress.paths, transfer_id=progress.transfer_id))
        else:
            toast.set_progress(progress)
        position_received_toasts()

    def remove_received_toast(toast: ReceiveToast) -> None:
        if toast in active_toasts:
            active_toasts.remove(toast)
        for transfer_id, active_toast in list(receive_toasts_by_id.items()):
            if active_toast is toast:
                receive_toasts_by_id.pop(transfer_id, None)
        if toast in internet_receive_toasts:
            internet_receive_toasts.remove(toast)
        position_received_toasts()

    def position_received_toasts() -> None:
        for index, toast in enumerate(reversed(active_toasts)):
            toast.show_near_tray(app, tray, index)

    def show_internet_receive_toast() -> ReceiveToast:
        toast = ReceiveToast()
        internet_receive_toasts.append(toast)
        active_toasts.append(toast)
        position_received_toasts()
        return toast

    ticket_windows: list[TicketWindow] = []
    ticket_input_windows: list[TicketInputWindow] = []
    mobile_share_windows: list[MobileShareWindow] = []
    mobile_receive_windows: list[MobileReceiveWindow] = []
    receive_progress_windows: list[ReceiveProgressWindow] = []
    send_windows: list[SendProgressWindow] = []

    def position_send_toasts() -> None:
        base_index = len(active_toasts)
        for index, window in enumerate(reversed(send_windows)):
            window.show_near_tray(app, tray, base_index + index)

    def start_send_task(peer, paths: list[Path]) -> None:
        cancel_event = threading.Event()

        def retry() -> None:
            start_send_task(peer, list(paths))

        window = SendProgressWindow(
            peer.identity.device_name,
            paths,
            on_cancel=cancel_event.set,
            on_retry=retry,
        )
        send_windows.append(window)
        window.destroyed.connect(lambda *_: remove_send_window(window))
        position_send_toasts()

        def worker() -> None:
            failed = False
            cancelled = False
            error_message = ""
            try:
                send_paths(
                    peer.address,
                    peer.identity.listen_port,
                    paths,
                    on_progress=lambda progress: ui_events.send_progress.emit(
                        window,
                        progress,
                        peer.identity.device_name,
                    ),
                    cancel_event=cancel_event,
                )
            except InterruptedError:
                cancelled = True
            except Exception as exc:
                failed = True
                error_message = str(exc)
                print(f"LocalNetFTP send failed to {peer.identity.device_name}: {exc}", file=sys.stderr)
            finally:
                ui_events.send_finished.emit(
                    window,
                    peer.identity.device_name,
                    failed,
                    cancelled,
                    error_message,
                )

        threading.Thread(
            target=worker,
            name=f"LocalNetFTPSend-{peer.identity.device_id}",
            daemon=True,
        ).start()

    def remove_send_window(window: SendProgressWindow) -> None:
        if window in send_windows:
            send_windows.remove(window)
            position_send_toasts()

    def update_send_progress(window: SendProgressWindow, progress: TransferProgress, peer_name: str) -> None:
        if window not in send_windows:
            return
        window.set_progress(progress)
        floating_window._set_lan_progress(peer_name, progress)

    def finish_send_window(
        window: SendProgressWindow,
        peer_name: str,
        failed: bool,
        cancelled: bool,
        error_message: str,
    ) -> None:
        if cancelled:
            window.finish_cancelled()
            floating_window.transfer_status.setText(f"{peer_name} 已取消")
            return
        if failed:
            window.finish_failed(error_message)
            floating_window.transfer_status.setText(f"{peer_name} 失败：{error_message}")
            return
        window.finish_success()
        floating_window._finish_progress("发送完成")

    def show_ticket_window(provider: IrohTicketProvider, ticket: InternetTicket) -> None:
        floating_window.transfer_status.setText("公网 ticket 已生成")
        window = TicketWindow(runtime, provider, ticket)
        ticket_windows.append(window)
        window.destroyed.connect(lambda: ticket_windows.remove(window) if window in ticket_windows else None)
        window.setWindowIcon(icon)
        bring_window_to_front(window)

    def show_mobile_share_window(paths: list[Path]) -> None:
        for window in list(mobile_share_windows):
            window.close()
        try:
            server = runtime.start_mobile_share_server(paths)
        except Exception as exc:
            floating_window._reset_progress()
            QMessageBox.warning(None, "LocalNetFTP", f"手机网页下载启动失败：{type(exc).__name__}: {exc}")
            return
        floating_window._finish_progress("手机网页下载已生成")
        window = MobileShareWindow(runtime, server)
        mobile_share_windows.append(window)
        window.destroyed.connect(
            lambda *_: mobile_share_windows.remove(window) if window in mobile_share_windows else None
        )
        window.setWindowIcon(icon)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_mobile_receive_window() -> None:
        runtime.log_debug("show_mobile_receive_window entered")
        try:
            for window in list(mobile_receive_windows):
                if not window.is_closed:
                    runtime.log_debug("mobile receive window already exists; bringing to front")
                    bring_window_to_front(window)
                    return
            window = MobileReceiveWindow(runtime)
            mobile_receive_windows.append(window)
            window.destroyed.connect(
                lambda *_: mobile_receive_windows.remove(window) if window in mobile_receive_windows else None
            )
            window.setWindowIcon(icon)
            bring_window_to_front(window)
            QApplication.processEvents()
            runtime.log_debug("mobile receive loading window shown")
        except Exception as exc:
            runtime.log_debug(f"mobile receive window open failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            QMessageBox.warning(None, "LocalNetFTP", f"从手机接收窗口打开失败：{type(exc).__name__}: {exc}")
            return

        runtime.log_debug("mobile receive initialization scheduled")
        QTimer.singleShot(120, lambda: start_mobile_receive_initialization(window))

    def start_mobile_receive_initialization(window: MobileReceiveWindow) -> None:
        if window.is_closed or window not in mobile_receive_windows:
            runtime.log_debug("mobile receive initialization skipped because window is closed")
            return
        runtime.log_debug("mobile receive initialization thread starting")

        def worker() -> None:
            try:
                runtime.log_debug("mobile receive worker starting server")
                server = runtime.start_mobile_receive_server()
                runtime.log_debug("mobile receive worker collecting urls")
                urls = server.urls()
            except Exception as exc:
                runtime.log_debug(f"mobile receive worker failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                ui_events.mobile_receive_error.emit(window, f"从手机接收启动失败：{type(exc).__name__}: {exc}")
                return
            runtime.log_debug(f"mobile receive worker ready with {len(urls)} urls")
            ui_events.mobile_receive_ready.emit(window, server, urls)

        threading.Thread(
            target=worker,
            name="LocalNetFTPMobileReceiveStart",
            daemon=True,
        ).start()

    def bring_window_to_front(window: QWidget) -> None:
        window.show()
        window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        window.raise_()
        window.activateWindow()
        QTimer.singleShot(0, lambda: (window.show(), window.raise_(), window.activateWindow()))

    def handle_mobile_receive_ready(
        window: MobileReceiveWindow,
        server: MobileReceiveServer,
        urls: list[ShareAddress],
    ) -> None:
        runtime.log_debug("mobile receive ready signal received")
        if window.is_closed or window not in mobile_receive_windows:
            runtime.log_debug("mobile receive ready ignored because window is closed")
            runtime.stop_mobile_receive_server_async()
            return
        window.set_ready(server, urls)

    def handle_mobile_receive_error(window: MobileReceiveWindow, message: str) -> None:
        runtime.log_debug(f"mobile receive error signal received: {message}")
        runtime.stop_mobile_receive_server_async()
        if window.is_closed or window not in mobile_receive_windows:
            return
        window.set_error(message)

    def show_receive_progress_window() -> ReceiveProgressWindow:
        window = ReceiveProgressWindow()
        receive_progress_windows.append(window)
        window.destroyed.connect(
            lambda: receive_progress_windows.remove(window) if window in receive_progress_windows else None
        )
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def show_ticket_input_window() -> None:
        def submit(ticket: str) -> None:
            show_internet_receive_toast()
            runtime.receive_internet_ticket(ticket)

        window = TicketInputWindow(submit)
        ticket_input_windows.append(window)
        window.destroyed.connect(
            lambda *_: ticket_input_windows.remove(window) if window in ticket_input_windows else None
        )
        window.setWindowIcon(icon)
        window.show()
        window.raise_()
        window.activateWindow()

    def handle_internet_progress(progress: InternetTransferProgress) -> None:
        floating_window.set_internet_progress(progress)
        if progress.role == "send":
            for window in list(ticket_windows):
                window.set_progress(progress)
            return
        for toast in list(internet_receive_toasts):
            toast.set_internet_progress(progress)
        for window in list(receive_progress_windows):
            window.set_progress(progress)

    def show_error_message(message: str) -> None:
        QMessageBox.warning(None, "LocalNetFTP", message)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ui_events = UiEvents()
    ui_events.received.connect(show_received_prompt)
    ui_events.receive_progress.connect(show_receive_progress)
    ui_events.send_progress.connect(update_send_progress)
    ui_events.send_finished.connect(finish_send_window)
    ui_events.error.connect(show_error_message)
    ui_events.internet_ticket.connect(show_ticket_window)
    ui_events.internet_progress.connect(handle_internet_progress)
    ui_events.mobile_receive_ready.connect(handle_mobile_receive_ready)
    ui_events.mobile_receive_error.connect(handle_mobile_receive_error)

    runtime = AppRuntime()
    startup_status = runtime.start()

    floating_window = FloatingWindow(runtime)
    floating_window.set_status(startup_status)

    def on_settings_saved(status: str) -> None:
        floating_window.reload_device_name()
        floating_window.set_status(status)

    settings_window = SettingsWindow(runtime, on_settings_saved)
    share_window = ShareWindow()

    icon = app.style().standardIcon(QStyle.SP_DriveNetIcon)
    floating_window.setWindowIcon(icon)
    settings_window.setWindowIcon(icon)
    share_window.setWindowIcon(icon)

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("LocalNetFTP")

    menu = QMenu()
    share_action = QAction("投送模式", menu)
    mobile_receive_action = QAction("从手机接收", menu)
    receive_ticket_action = QAction("输入 ticket 接收文件", menu)
    settings_action = QAction("设置", menu)
    quit_action = QAction("退出", menu)
    menu.addAction(share_action)
    menu.addAction(mobile_receive_action)
    menu.addAction(receive_ticket_action)
    menu.addAction(settings_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    def show_floating_window() -> None:
        floating_window.apply_initial_position(app)
        floating_window.show()
        floating_window.raise_()
        floating_window.activateWindow()

    ui_events.show_floating.connect(show_floating_window)

    def show_settings_window(checked: bool = False) -> None:
        settings_window.reload()
        settings_window.show()
        settings_window.raise_()
        settings_window.activateWindow()

    def show_share_window(checked: bool = False) -> None:
        try:
            server = runtime.start_share_server()
            share_window.set_urls(server.urls())
        except Exception as exc:
            QMessageBox.warning(None, "LocalNetFTP", f"投送模式启动失败：{type(exc).__name__}: {exc}")
            return
        share_window.show()
        share_window.raise_()
        share_window.activateWindow()

    def receive_ticket(checked: bool = False) -> None:
        show_ticket_input_window()

    def open_mobile_receive_from_tray(checked: bool = False) -> None:
        runtime.log_debug(f"mobile receive tray action triggered checked={checked}")
        try:
            QTimer.singleShot(0, show_mobile_receive_window)
            runtime.log_debug("mobile receive tray action scheduled")
        except Exception as exc:
            runtime.log_debug(f"mobile receive tray action failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            QMessageBox.warning(None, "LocalNetFTP", f"从手机接收启动失败：{type(exc).__name__}: {exc}")

    share_action.triggered.connect(show_share_window)
    mobile_receive_action.triggered.connect(open_mobile_receive_from_tray)
    receive_ticket_action.triggered.connect(receive_ticket)
    settings_action.triggered.connect(show_settings_window)
    quit_action.triggered.connect(app.quit)

    def on_tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            show_floating_window()

    tray.activated.connect(on_tray_activated)
    tray.show()
    app.aboutToQuit.connect(runtime.stop)

    return app.exec()


def _current_executable() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.argv[0]).resolve()


def _share_executable_path() -> Path:
    candidates = []
    for runtime_path in (Path(sys.executable), Path(sys.argv[0]).resolve()):
        if runtime_path.name.lower() == "localnetftp.exe":
            candidates.append(runtime_path)
    candidates.extend(
        [
            Path.cwd() / "LocalNetFTP.exe",
            Path.cwd() / "dist" / "LocalNetFTP.exe",
            Path(__file__).resolve().parents[3] / "dist" / "LocalNetFTP.exe",
        ]
    )
    for candidate in candidates:
        if candidate.suffix.lower() == ".exe" and candidate.exists():
            return candidate
    return candidates[0]


def _received_message(paths: list[Path]) -> str:
    if len(paths) == 1:
        return f"已保存：{paths[0].name}"
    return f"已保存 {len(paths)} 个项目：\n" + "\n".join(path.name for path in paths[:8])


def _send_items_text(paths: list[Path]) -> str:
    if len(paths) == 1:
        return paths[0].name
    names = "、".join(path.name for path in paths[:3])
    remaining_count = len(paths) - 3
    if remaining_count > 0:
        return f"{names} 等 {remaining_count} 个"
    return names


def _transfer_progress_percent(done: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round(done * 100 / total)))


def _is_image_preview_path(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def _is_text_preview_path(path: Path) -> bool:
    return path.suffix.lower() in {".txt", ".md", ".log", ".csv", ".json", ".py", ".ini", ".yaml", ".yml"}


def _read_text_preview(path: Path, max_bytes: int = 64 * 1024) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        data = data[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gbk")
        except UnicodeDecodeError:
            return None
    if len(text) > 4000:
        return f"{text[:4000]}\n..."
    return text


def _open_item_button_text(paths: list[Path]) -> str:
    if len(paths) == 1 and paths[0].is_dir():
        return "打开文件夹"
    if len(paths) == 1:
        return "打开文件"
    return "打开第一个项目"


def _open_save_location(paths: list[Path]) -> None:
    first_path = paths[0]
    if first_path.is_file():
        subprocess.Popen(["explorer", f"/select,{first_path}"])
        return
    _open_path(first_path.parent)


def _open_received_item(paths: list[Path]) -> None:
    _open_path(paths[0])


def _open_path(path: Path) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


def _show_widget_near_tray(widget, app, tray_icon, stack_index: int = 0) -> None:
    widget.adjustSize()
    margin = 16
    gap = 8

    tray_geometry = tray_icon.geometry() if tray_icon is not None else None
    has_tray_geometry = (
        tray_geometry is not None
        and tray_geometry.isValid()
        and not tray_geometry.isNull()
    )
    screen = app.screenAt(tray_geometry.center()) if has_tray_geometry else app.primaryScreen()
    if screen is None:
        widget.show()
        return

    work_area = _work_area_bounds(screen)
    x = work_area["right"] - widget.width() - margin
    y = work_area["bottom"] - widget.height() + 1 - stack_index * (widget.height() + gap)
    min_x = work_area["left"] + margin
    max_x = work_area["right"] - widget.width() - margin
    min_y = work_area["top"] + margin
    max_y = work_area["bottom"] - widget.height() + 1
    widget.move(_clamp(x, min_x, max_x), _clamp(y, min_y, max_y))
    widget.show()


def _work_area_bounds(screen) -> dict[str, int]:
    if sys.platform == "win32":
        windows_bounds = _windows_work_area_bounds()
        if windows_bounds is not None:
            return windows_bounds

    available = screen.availableGeometry()
    return {
        "left": available.left(),
        "top": available.top(),
        "right": available.right(),
        "bottom": available.bottom(),
    }


def _windows_work_area_bounds() -> dict[str, int] | None:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    rect = wintypes.RECT()
    SPI_GETWORKAREA = 0x0030
    if not ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return None
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right - 1,
        "bottom": rect.bottom - 1,
    }


def _clamp(value: int, minimum: int, maximum: int) -> int:
    if maximum < minimum:
        return minimum
    return max(minimum, min(maximum, value))


def _append_debug_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        path.open("a", encoding="utf-8").write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _internet_progress_percent(progress: InternetTransferProgress) -> int:
    if progress.bytes_total > 0:
        return max(0, min(100, round(progress.bytes_done * 100 / progress.bytes_total)))
    if progress.stage in ("done", "serving"):
        return 100
    return 0


def _internet_progress_text(progress: InternetTransferProgress) -> str:
    percent = _internet_progress_percent(progress)
    if progress.bytes_total > 0:
        return f"{progress.message} {percent}%"
    return progress.message


def _toast_stylesheet() -> str:
    return """
    QWidget {
        background-color: rgba(250, 252, 255, 245);
        color: #172033;
        border: 1px solid rgba(132, 146, 166, 120);
        border-radius: 8px;
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 12px;
    }
    QLabel {
        border: 0;
        background-color: transparent;
    }
    QLabel#toastTitle {
        font-size: 14px;
        font-weight: 600;
    }
    QLabel#toastMessage {
        color: #465568;
    }
    QLabel#toastImagePreview {
        min-height: 80px;
        max-height: 180px;
        border-radius: 6px;
        background-color: rgba(241, 245, 249, 230);
    }
    QWidget#toastPreviewContainer {
        border: 0;
        background-color: transparent;
    }
    QPlainTextEdit#toastTextPreview {
        padding: 6px;
        border: 1px solid rgba(148, 163, 184, 150);
        border-radius: 6px;
        background-color: rgba(255, 255, 255, 235);
        color: #172033;
        selection-background-color: #bfdbfe;
    }
    QProgressBar#toastProgress {
        min-height: 7px;
        max-height: 7px;
        border: 0;
        border-radius: 3px;
        background-color: rgba(210, 218, 228, 190);
        text-align: center;
    }
    QProgressBar#toastProgress::chunk {
        border-radius: 3px;
        background-color: #2f7dd1;
    }
    QPushButton {
        min-height: 26px;
        padding: 2px 10px;
        border-radius: 5px;
        border: 1px solid #9aa8ba;
        background-color: rgba(255, 255, 255, 235);
    }
    QPushButton:hover {
        background-color: #eef5ff;
    }
    QPushButton#toastClose {
        min-width: 22px;
        max-width: 22px;
        min-height: 22px;
        max-height: 22px;
        padding: 0;
        border-radius: 11px;
        border: 0;
        background-color: transparent;
        font-size: 16px;
    }
    QPushButton#toastClose:hover {
        background-color: rgba(226, 232, 240, 220);
    }
    """


def _app_stylesheet() -> str:
    return """
    QWidget {
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 13px;
    }
    QLabel#titleLabel {
        font-size: 22px;
        font-weight: 600;
    }
    QLabel#mutedLabel,
    QLabel#statusLabel {
        color: #536173;
    }
    QLineEdit {
        min-height: 28px;
        padding: 3px 6px;
    }
    QPlainTextEdit {
        padding: 8px;
        border: 1px solid rgba(148, 163, 184, 150);
        border-radius: 6px;
        background-color: rgba(255, 255, 255, 245);
        selection-background-color: #bfdbfe;
    }
    QListWidget {
        min-height: 120px;
    }
    QWidget#shareAddressCard {
        border: 1px solid rgba(148, 163, 184, 130);
        border-radius: 8px;
        background-color: rgba(255, 255, 255, 245);
    }
    QLabel#shareInterfaceLabel {
        font-weight: 600;
        color: #172033;
    }
    QPushButton#linkButton {
        min-height: 30px;
        padding: 3px 8px;
        border: 1px solid transparent;
        background-color: transparent;
        color: #2f7dd1;
        text-align: left;
    }
    QPushButton#linkButton:hover {
        border-color: rgba(47, 125, 209, 90);
        background-color: #eef5ff;
    }
    QProgressBar#inlineProgress {
        min-height: 8px;
        max-height: 8px;
        border: 0;
        border-radius: 4px;
        background-color: rgba(210, 218, 228, 190);
        text-align: center;
    }
    QProgressBar#inlineProgress::chunk {
        border-radius: 4px;
        background-color: #2f7dd1;
    }
    QPushButton {
        min-height: 30px;
        padding: 3px 14px;
        border-radius: 5px;
        border: 1px solid #9aa8ba;
        background-color: rgba(255, 255, 255, 240);
    }
    QPushButton:hover {
        background-color: #eef5ff;
    }
    """


def _floating_stylesheet() -> str:
    return """
    QWidget {
        background-color: rgba(248, 250, 252, 235);
        color: #172033;
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 12px;
    }
    QLineEdit#floatingName {
        min-height: 28px;
        padding: 3px 8px;
        border: 1px solid transparent;
        border-radius: 6px;
        background-color: transparent;
        font-weight: 600;
    }
    QLineEdit#floatingName:focus {
        border: 1px solid rgba(132, 146, 166, 140);
        background-color: rgba(255, 255, 255, 230);
    }
    QPushButton#floatingClose {
        min-width: 24px;
        max-width: 24px;
        min-height: 24px;
        max-height: 24px;
        padding: 0;
        border-radius: 12px;
        border: 0;
        background-color: transparent;
        color: #475569;
        font-size: 16px;
    }
    QPushButton#floatingClose:hover {
        background-color: rgba(226, 232, 240, 220);
        color: #b91c1c;
    }
    QLineEdit#pasteInput {
        min-height: 28px;
        padding: 3px 8px;
        border: 1px solid rgba(132, 146, 166, 130);
        border-radius: 6px;
        background-color: rgba(255, 255, 255, 220);
        color: #334155;
    }
    QLabel#transferStatus {
        color: #536173;
        font-size: 11px;
        min-height: 18px;
    }
    QProgressBar#transferProgress {
        min-height: 6px;
        max-height: 6px;
        border: 0;
        border-radius: 3px;
        background-color: rgba(210, 218, 228, 190);
        text-align: center;
    }
    QProgressBar#transferProgress::chunk {
        border-radius: 3px;
        background-color: #2f7dd1;
    }
    QListWidget {
        min-height: 180px;
        border: 1px solid rgba(132, 146, 166, 150);
        background-color: rgba(255, 255, 255, 225);
        alternate-background-color: rgba(240, 244, 248, 215);
        border-radius: 6px;
        padding: 4px;
        outline: 0;
    }
    QListWidget::item {
        min-height: 24px;
        padding: 3px 6px;
        border-radius: 4px;
    }
    QListWidget::item:selected {
        background-color: #2f7dd1;
        color: white;
    }
    QPushButton {
        min-height: 26px;
        padding: 2px 12px;
        border-radius: 5px;
        border: 1px solid #9aa8ba;
        background-color: rgba(255, 255, 255, 230);
    }
    QPushButton:hover {
        background-color: #eef5ff;
    }
    QPushButton:disabled {
        color: #8b95a3;
        background-color: rgba(231, 235, 240, 210);
    }
    """
