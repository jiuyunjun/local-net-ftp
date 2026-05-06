from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
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
from localnetftp.share import DownloadShareServer, ShareAddress
from localnetftp.transfer import ReceiveResult, TransferProgress, TransferServer, send_paths
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


def run_tray_app(options: RuntimeOptions | None = None) -> int:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal
    from PySide6.QtGui import QAction, QKeySequence
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMenu,
        QMessageBox,
        QPushButton,
        QInputDialog,
        QPlainTextEdit,
        QProgressBar,
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    SHARE_PORT = 49300
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
        def __init__(self, on_paste) -> None:
            super().__init__()
            self._on_paste = on_paste
            self.setObjectName("pasteInput")
            self.setPlaceholderText("粘贴文字 / 图片 / 文件")

        def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.matches(QKeySequence.Paste):
                self._on_paste()
                event.accept()
                return
            super().keyPressEvent(event)

    class UiEvents(QObject):
        received = Signal(object)
        error = Signal(str)
        internet_ticket = Signal(object, object)
        internet_progress = Signal(object)

    class AppRuntime:
        def __init__(self) -> None:
            self.options = runtime_options
            self.config_path = runtime_options.config_path
            self.config_dir = runtime_options.config_dir or default_config_dir()
            self.config = self._load_initial_config()
            self.discovery_service: DiscoveryService | None = None
            self.transfer_server: TransferServer | None = None
            self.share_server: DownloadShareServer | None = None
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
            save_config(self.config, self.config_path)

        def _load_initial_config(self) -> AppConfig:
            if self.options.dev_instance and self.config_path is not None and not self.config_path.exists():
                config = AppConfig(
                    receive_dir=self.config_dir / "Downloads",
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
            return ""

        def stop(self) -> None:
            self.stop_discovery()
            self.stop_transfer_server()
            self.stop_share_server()
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
                self.share_server = DownloadShareServer(_share_executable_path(), port=SHARE_PORT)
                self.share_server.start()
            return self.share_server

        def stop_share_server(self) -> None:
            if self.share_server is not None:
                self.share_server.stop()
                self.share_server = None

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
            self._internet_row: int | None = None
            self._active_send_count = 0
            self._send_failures: list[str] = []
            self._send_lock = threading.Lock()

            self.setWindowTitle("LocalNetFTP")
            self.setMinimumSize(260, 220)
            self.setMaximumWidth(320)
            self.setAcceptDrops(True)
            self.setWindowOpacity(0.92)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setWindowFlag(Qt.Tool, True)
            self._initial_position_applied = False

            self.device_name = NameEdit(self._runtime.config.device_name)
            self.device_name.editingFinished.connect(self._save_device_name)

            self.peer_list = QListWidget()
            self.peer_list.setAlternatingRowColors(True)
            self.peer_list.setSelectionMode(QListWidget.ExtendedSelection)
            self.peer_list.setToolTip("同一局域网内运行 LocalNetFTP 的电脑会显示在这里")
            self.peer_list.itemSelectionChanged.connect(self._update_send_button)

            self.paste_input = PasteInput(self._paste_clipboard)
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
            layout.addWidget(self.device_name)
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
                device_name=device_name,
                device_id=self._runtime.config.device_id,
            )
            self.set_status(self._runtime.save_settings(config))

        def _paste_clipboard(self) -> None:
            paths = self._paths_from_clipboard()
            if paths:
                self.paste_input.clear()
                self._confirm_and_send(paths)

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

        def _update_send_button(self) -> None:
            return

        def _confirm_and_send(self, paths: list[Path]) -> None:
            peers = self._selected_peers()
            peer_names = [peer.identity.device_name for peer in peers]
            internet_selected = self._is_internet_selected()
            display_names = list(peer_names)
            if internet_selected:
                display_names.append("局域网外用户")
            if not can_send(len(display_names), len(paths)):
                QMessageBox.information(self, "LocalNetFTP", "请先选择要发送给谁。")
                self._update_send_button()
                return

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

            if internet_selected:
                self.transfer_status.setText("正在生成公网 ticket...")
                self._show_progress(0)
                self._runtime.start_internet_provider(paths)

            if peers:
                with self._send_lock:
                    self._active_send_count = len(peers)
                    self._send_failures = []

                print(f"LocalNetFTP: {send_summary(peer_names, paths)}", file=sys.stderr)
                self._update_send_button()
                for peer in peers:
                    threading.Thread(
                        target=self._send_to_peer,
                        args=(peer, list(paths)),
                        name=f"LocalNetFTPSend-{peer.identity.device_id}",
                        daemon=True,
                    ).start()

        def _send_to_peer(self, peer, paths: list[Path]) -> None:
            failed = False
            error_message = ""
            try:
                send_paths(
                    peer.address,
                    peer.identity.listen_port,
                    paths,
                    on_progress=lambda progress: self._send_progress(peer.identity.device_name, progress),
                )
            except Exception as exc:
                failed = True
                error_message = str(exc)
                print(f"LocalNetFTP send failed to {peer.identity.device_name}: {exc}", file=sys.stderr)
                with self._send_lock:
                    self._send_failures.append(peer.identity.device_name)
            finally:
                QTimer.singleShot(
                    0,
                    lambda: self._send_finished(peer.identity.device_name, failed, error_message),
                )

        def _send_progress(self, peer_name: str, progress: TransferProgress) -> None:
            QTimer.singleShot(
                0,
                lambda: self._set_lan_progress(peer_name, progress),
            )

        def _set_lan_progress(self, peer_name: str, progress: TransferProgress) -> None:
            self.transfer_status.setText(
                f"{peer_name}: {progress.item_index}/{progress.item_count} {progress.relative_path}"
            )
            if progress.total_bytes > 0:
                self._show_progress(round(progress.bytes_sent * 100 / progress.total_bytes))

        def _send_finished(self, peer_name: str, failed: bool, error_message: str = "") -> None:
            with self._send_lock:
                self._active_send_count = max(0, self._active_send_count - 1)
                active_send_count = self._active_send_count
                failures = list(self._send_failures)

            if active_send_count:
                return

            if failures:
                print(f"LocalNetFTP: 发送完成，失败：{'、'.join(failures)}", file=sys.stderr)
                self.transfer_status.setText(f"失败：{'、'.join(failures)} {error_message}".strip())
            elif failed:
                print(f"LocalNetFTP: 发送到 {peer_name} 失败。", file=sys.stderr)
                self.transfer_status.setText(f"{peer_name} 失败：{error_message}")
            else:
                print("LocalNetFTP: 发送完成。", file=sys.stderr)
                self.transfer_status.setText("发送完成")
                self._show_progress(100)
            self._update_send_button()

        def set_internet_progress(self, progress: InternetTransferProgress) -> None:
            if progress.bytes_total > 0:
                percent = round(progress.bytes_done * 100 / progress.bytes_total)
                self.transfer_status.setText(f"{progress.message} {percent}%")
                self._show_progress(percent)
                return
            self.transfer_status.setText(progress.message)
            if progress.stage in ("done", "serving"):
                self._show_progress(100)
            else:
                self._show_progress(0)

        def _show_progress(self, value: int) -> None:
            self.transfer_progress.show()
            self.transfer_progress.setValue(max(0, min(100, value)))

        def _refresh_peers(self) -> None:
            selected_names = set(self._selected_peer_names())
            self.peer_list.clear()
            self._peers_by_row = {}
            self._internet_row = self.peer_list.count()
            self.peer_list.addItem("局域网外用户（生成 ticket）")
            if "局域网外用户" in selected_names:
                self.peer_list.item(self._internet_row).setSelected(True)

            peers = self._runtime.peers()
            if not peers:
                self._update_send_button()
                return

            for peer in peers:
                row = self.peer_list.count()
                peer_name = peer.identity.device_name
                self._peers_by_row[row] = peer
                self.peer_list.addItem(f"{peer_name}  {peer.address}:{peer.identity.listen_port}")
                if peer_name in selected_names:
                    self.peer_list.item(row).setSelected(True)
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

            save_button = QPushButton("保存设置")
            save_button.clicked.connect(self._save)

            layout = QVBoxLayout(self)
            layout.addWidget(device_label)
            layout.addWidget(self.device_name)
            layout.addWidget(receive_label)
            layout.addLayout(receive_layout)
            layout.addWidget(self.start_on_boot)
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

    class TicketWindow(QWidget):
        def __init__(self, runtime: AppRuntime, provider: IrohTicketProvider, ticket: InternetTicket) -> None:
            super().__init__()
            self._runtime = runtime
            self._provider = provider
            self.setWindowTitle("公网 ticket")
            self.setMinimumSize(560, 260)

            label = QLabel("把这个 ticket 发给对方。窗口关闭前，文件会保持可下载。")
            self.ticket_text = QPlainTextEdit(ticket.ticket)
            self.ticket_text.setReadOnly(True)

            copy_button = QPushButton("复制 ticket")
            copy_button.clicked.connect(self._copy_ticket)
            close_button = QPushButton("停止分享")
            close_button.clicked.connect(self.close)

            button_layout = QHBoxLayout()
            button_layout.addWidget(copy_button)
            button_layout.addWidget(close_button)

            layout = QVBoxLayout(self)
            layout.addWidget(label)
            layout.addWidget(self.ticket_text, 1)
            layout.addLayout(button_layout)
            self.setStyleSheet(_app_stylesheet())

        def _copy_ticket(self) -> None:
            QApplication.clipboard().setText(self.ticket_text.toPlainText())

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
            self._runtime.stop_internet_provider(self._provider)
            super().closeEvent(event)

    class ReceiveToast(QWidget):
        def __init__(self, result: ReceiveResult) -> None:
            super().__init__()
            self.paths = result.paths
            self.setWindowTitle("收到文件")
            self.setWindowFlag(Qt.Tool, True)
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setFixedWidth(320)

            title = QLabel("收到文件")
            title.setObjectName("toastTitle")
            close_button = QPushButton("×")
            close_button.setObjectName("toastClose")
            close_button.setToolTip("关闭")
            close_button.clicked.connect(self.close)

            title_layout = QHBoxLayout()
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.addWidget(title, 1)
            title_layout.addWidget(close_button)

            message = QLabel(_received_message(self.paths))
            message.setObjectName("toastMessage")
            message.setWordWrap(True)

            open_folder = QPushButton("打开保存位置")
            open_folder.clicked.connect(self._open_save_location)
            open_item = QPushButton(_open_item_button_text(self.paths))
            open_item.clicked.connect(self._open_received_item)

            button_layout = QHBoxLayout()
            button_layout.addWidget(open_folder)
            button_layout.addWidget(open_item)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 10, 12, 12)
            layout.setSpacing(8)
            layout.addLayout(title_layout)
            layout.addWidget(message)
            layout.addLayout(button_layout)

            self.setStyleSheet(_toast_stylesheet())
            QTimer.singleShot(6500, self.close)

        def show_near_tray(self, app: QApplication) -> None:
            screen = app.primaryScreen()
            if screen is None:
                self.show()
                return
            self.adjustSize()
            geometry = screen.availableGeometry()
            margin = 16
            x = geometry.right() - self.width() - margin
            y = geometry.bottom() - self.height() - margin
            self.move(max(geometry.left(), x), max(geometry.top(), y))
            self.show()

        def _open_save_location(self) -> None:
            _open_save_location(self.paths)
            self.close()

        def _open_received_item(self) -> None:
            _open_received_item(self.paths)
            self.close()

    active_toasts: list[ReceiveToast] = []

    def show_received_prompt(result: ReceiveResult) -> None:
        paths = result.paths
        if not paths:
            return

        toast = ReceiveToast(result)
        active_toasts.append(toast)
        toast.destroyed.connect(lambda: active_toasts.remove(toast) if toast in active_toasts else None)
        toast.show_near_tray(app)

    ticket_windows: list[TicketWindow] = []

    def show_ticket_window(provider: IrohTicketProvider, ticket: InternetTicket) -> None:
        floating_window.transfer_status.setText("公网 ticket 已生成")
        window = TicketWindow(runtime, provider, ticket)
        ticket_windows.append(window)
        window.destroyed.connect(lambda: ticket_windows.remove(window) if window in ticket_windows else None)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_error_message(message: str) -> None:
        QMessageBox.warning(None, "LocalNetFTP", message)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ui_events = UiEvents()
    ui_events.received.connect(show_received_prompt)
    ui_events.error.connect(show_error_message)
    ui_events.internet_ticket.connect(show_ticket_window)
    ui_events.internet_progress.connect(lambda progress: floating_window.set_internet_progress(progress))

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
    receive_ticket_action = QAction("输入 ticket 接收文件", menu)
    settings_action = QAction("设置", menu)
    quit_action = QAction("退出", menu)
    menu.addAction(share_action)
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

    def show_settings_window() -> None:
        settings_window.reload()
        settings_window.show()
        settings_window.raise_()
        settings_window.activateWindow()

    def show_share_window() -> None:
        try:
            server = runtime.start_share_server()
            share_window.set_urls(server.urls())
        except Exception as exc:
            QMessageBox.warning(None, "LocalNetFTP", f"投送模式启动失败：{type(exc).__name__}: {exc}")
            return
        share_window.show()
        share_window.raise_()
        share_window.activateWindow()

    def receive_ticket() -> None:
        ticket, accepted = QInputDialog.getMultiLineText(None, "输入 ticket", "粘贴对方发来的 ticket：")
        if accepted and ticket.strip():
            runtime.receive_internet_ticket(ticket)

    share_action.triggered.connect(show_share_window)
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
    QLineEdit {
        min-height: 28px;
        padding: 3px 6px;
    }
    QListWidget {
        min-height: 120px;
    }
    QPushButton {
        min-height: 30px;
        padding: 3px 14px;
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
