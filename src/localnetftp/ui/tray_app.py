from __future__ import annotations

import sys
import threading
from pathlib import Path

from localnetftp.config import (
    AppConfig,
    default_config_dir,
    is_start_on_boot_enabled,
    load_config,
    save_config,
    set_start_on_boot,
)
from localnetftp.network import DiscoveryService, create_device_identity
from localnetftp.share import DownloadShareServer, ShareAddress
from localnetftp.transfer import TransferServer, send_paths
from localnetftp.ui.clipboard_payload import timestamped_clipboard_path
from localnetftp.ui.drop_paths import local_paths_from_urls
from localnetftp.ui.send_state import can_send, confirmation_text, send_summary


TRANSFER_LISTEN_PORT = 49200


def run_tray_app() -> int:
    from PySide6.QtCore import QTimer, Qt
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
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    SHARE_PORT = 49300

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

    class AppRuntime:
        def __init__(self) -> None:
            self.config = load_config()
            save_config(self.config)
            self.discovery_service: DiscoveryService | None = None
            self.transfer_server: TransferServer | None = None
            self.share_server: DownloadShareServer | None = None

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

        def save_settings(self, config: AppConfig) -> str:
            save_config(config)
            self.config = config
            set_start_on_boot(
                config.start_on_boot,
                _current_executable(),
                app_name="LocalNetFTP",
            )
            self.stop()
            return self.start()

        def start_transfer_server(self) -> str:
            self.transfer_server = TransferServer(self.config.receive_dir, TRANSFER_LISTEN_PORT)
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
            identity = create_device_identity(
                self.config.device_name,
                listen_port=TRANSFER_LISTEN_PORT,
                device_id=self.config.device_id,
            )
            self.discovery_service = DiscoveryService(identity)
            try:
                self.discovery_service.start()
            except OSError as exc:
                self.discovery_service = None
                return f"局域网发现启动失败：{exc}"
            return ""

        def stop_discovery(self) -> None:
            if self.discovery_service is not None:
                self.discovery_service.stop()
                self.discovery_service = None

        def start_share_server(self) -> DownloadShareServer:
            if self.share_server is None:
                self.share_server = DownloadShareServer(_share_executable_path(), port=SHARE_PORT)
                self.share_server.start()
            return self.share_server

        def stop_share_server(self) -> None:
            if self.share_server is not None:
                self.share_server.stop()
                self.share_server = None

    class FloatingWindow(QWidget):
        def __init__(self, runtime: AppRuntime) -> None:
            super().__init__()
            self._runtime = runtime
            self._peers_by_row = {}
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

            layout = QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)
            layout.addWidget(self.device_name)
            layout.addWidget(self.peer_list, 1)
            layout.addWidget(self.paste_input)

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

            clipboard_dir = default_config_dir() / "clipboard"
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
            return [peer.identity.device_name for peer in self._selected_peers()]

        def _selected_peers(self):
            peers = []
            for item in self.peer_list.selectedItems():
                row = self.peer_list.row(item)
                peer = self._peers_by_row.get(row)
                if peer is not None:
                    peers.append(peer)
            return peers

        def _update_send_button(self) -> None:
            return

        def _confirm_and_send(self, paths: list[Path]) -> None:
            peers = self._selected_peers()
            peer_names = [peer.identity.device_name for peer in peers]
            if not can_send(len(peers), len(paths)):
                QMessageBox.information(self, "LocalNetFTP", "请先选择要发送给谁。")
                self._update_send_button()
                return

            result = QMessageBox.question(
                self,
                "确认发送",
                confirmation_text(peer_names, paths),
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Ok,
            )
            if result != QMessageBox.Ok:
                self._update_send_button()
                return

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
            try:
                send_paths(peer.address, peer.identity.listen_port, paths)
            except Exception as exc:
                failed = True
                print(f"LocalNetFTP send failed to {peer.identity.device_name}: {exc}", file=sys.stderr)
                with self._send_lock:
                    self._send_failures.append(peer.identity.device_name)
            finally:
                QTimer.singleShot(0, lambda: self._send_finished(peer.identity.device_name, failed))

        def _send_finished(self, peer_name: str, failed: bool) -> None:
            with self._send_lock:
                self._active_send_count = max(0, self._active_send_count - 1)
                active_send_count = self._active_send_count
                failures = list(self._send_failures)

            if active_send_count:
                return

            if failures:
                print(f"LocalNetFTP: 发送完成，失败：{'、'.join(failures)}", file=sys.stderr)
            elif failed:
                print(f"LocalNetFTP: 发送到 {peer_name} 失败。", file=sys.stderr)
            else:
                print("LocalNetFTP: 发送完成。", file=sys.stderr)
            self._update_send_button()

        def _refresh_peers(self) -> None:
            selected_names = set(self._selected_peer_names())
            self.peer_list.clear()
            self._peers_by_row = {}

            discovery_service = self._runtime.discovery_service
            if discovery_service is None:
                self._update_send_button()
                return

            peers = discovery_service.peers()
            if not peers:
                self.peer_list.addItem("暂无在线用户")
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

            layout = QVBoxLayout(self)
            layout.addWidget(self.url_list)
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
            QMessageBox.information(self, "LocalNetFTP", "已复制下载地址。")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

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
    settings_action = QAction("设置", menu)
    quit_action = QAction("退出", menu)
    menu.addAction(share_action)
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

    share_action.triggered.connect(show_share_window)
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
