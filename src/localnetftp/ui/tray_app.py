from __future__ import annotations

import sys
import threading
from pathlib import Path

from localnetftp.config import (
    AppConfig,
    is_start_on_boot_enabled,
    load_config,
    save_config,
    set_start_on_boot,
)
from localnetftp.network import DiscoveryService, create_device_identity
from localnetftp.transfer import TransferServer, send_paths
from localnetftp.ui.drop_paths import append_unique_paths, local_paths_from_urls
from localnetftp.ui.send_state import can_send, send_summary


TRANSFER_LISTEN_PORT = 49200


def run_tray_app() -> int:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QAction, QIcon
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

    class FloatingWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("LocalNetFTP")
            self.setMinimumSize(520, 360)
            self.setAcceptDrops(True)

            self._config = load_config()
            save_config(self._config)
            self._pending_paths: list[Path] = []
            self._peers_by_row = {}
            self._discovery_service: DiscoveryService | None = None
            self._transfer_server: TransferServer | None = None
            self._active_send_count = 0
            self._send_failures: list[str] = []
            self._send_lock = threading.Lock()

            title = QLabel("LocalNetFTP")
            title.setObjectName("titleLabel")

            self.status = QLabel("正在启动局域网发现和文件接收服务。")
            self.status.setWordWrap(True)

            device_label = QLabel("本机名称")
            self.device_name = QLineEdit(self._config.device_name)
            self.device_name.setPlaceholderText("例如：客厅电脑")

            receive_label = QLabel("接收目录")
            self.receive_dir = QLineEdit(str(self._config.receive_dir))
            browse_button = QPushButton("浏览")
            browse_button.clicked.connect(self._choose_receive_dir)

            receive_layout = QHBoxLayout()
            receive_layout.addWidget(self.receive_dir, 1)
            receive_layout.addWidget(browse_button)

            self.start_on_boot = QCheckBox("开机自动启动")
            self.start_on_boot.setChecked(
                is_start_on_boot_enabled(_current_executable(), app_name="LocalNetFTP")
            )

            peers_label = QLabel("在线用户")
            self.peer_list = QListWidget()
            self.peer_list.setAlternatingRowColors(True)
            self.peer_list.setSelectionMode(QListWidget.ExtendedSelection)
            self.peer_list.setToolTip("同一局域网内运行 LocalNetFTP 的电脑会显示在这里")
            self.peer_list.itemSelectionChanged.connect(self._update_send_button)

            pending_label = QLabel("待发送")
            self.pending_list = QListWidget()
            self.pending_list.setAlternatingRowColors(True)
            self.pending_list.setSelectionMode(QListWidget.ExtendedSelection)
            self.pending_list.setToolTip("把文件或文件夹拖到窗口中")

            clear_button = QPushButton("清空列表")
            clear_button.clicked.connect(self._clear_pending_paths)

            self.send_button = QPushButton("发送")
            self.send_button.clicked.connect(self._send_selected)
            self.send_button.setEnabled(False)

            save_button = QPushButton("保存设置")
            save_button.clicked.connect(self._save)

            action_layout = QHBoxLayout()
            action_layout.addWidget(clear_button)
            action_layout.addStretch(1)
            action_layout.addWidget(self.send_button)
            action_layout.addWidget(save_button)

            layout = QVBoxLayout(self)
            layout.addWidget(title)
            layout.addWidget(self.status)
            layout.addSpacing(8)
            layout.addWidget(device_label)
            layout.addWidget(self.device_name)
            layout.addWidget(receive_label)
            layout.addLayout(receive_layout)
            layout.addWidget(self.start_on_boot)
            layout.addSpacing(8)
            layout.addWidget(peers_label)
            layout.addWidget(self.peer_list, 1)
            layout.addSpacing(8)
            layout.addWidget(pending_label)
            layout.addWidget(self.pending_list, 1)
            layout.addLayout(action_layout)

            self.setStyleSheet(
                """
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
            )
            self._peer_refresh_timer = QTimer(self)
            self._peer_refresh_timer.setInterval(1000)
            self._peer_refresh_timer.timeout.connect(self._refresh_peers)
            self._peer_refresh_timer.start()
            self._start_transfer_server()
            self._start_discovery()

        def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt method name
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event) -> None:  # noqa: N802 - Qt method name
            paths = local_paths_from_urls(event.mimeData().urls())
            if paths:
                self._set_pending_paths(append_unique_paths(self._pending_paths, paths))
                event.acceptProposedAction()
            else:
                event.ignore()

        def _choose_receive_dir(self) -> None:
            selected = QFileDialog.getExistingDirectory(self, "选择接收目录", self.receive_dir.text())
            if selected:
                self.receive_dir.setText(selected)

        def _clear_pending_paths(self) -> None:
            self._set_pending_paths([])

        def _set_pending_paths(self, paths: list[Path]) -> None:
            self._pending_paths = paths
            self.pending_list.clear()
            for path in paths:
                marker = "[文件夹]" if path.is_dir() else "[文件]"
                self.pending_list.addItem(f"{marker} {path}")
            self._update_send_button()

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
            self.send_button.setEnabled(
                self._active_send_count == 0
                and can_send(len(self._selected_peer_names()), len(self._pending_paths))
            )

        def _send_selected(self) -> None:
            peers = self._selected_peers()
            peer_names = [peer.identity.device_name for peer in peers]
            if not can_send(len(peers), len(self._pending_paths)):
                return
            with self._send_lock:
                self._active_send_count = len(peers)
                self._send_failures = []
            self.status.setText(send_summary(peer_names, self._pending_paths))
            self._update_send_button()
            for peer in peers:
                threading.Thread(
                    target=self._send_to_peer,
                    args=(peer,),
                    name=f"LocalNetFTPSend-{peer.identity.device_id}",
                    daemon=True,
                ).start()

        def _send_to_peer(self, peer) -> None:
            failed = False
            try:
                send_paths(peer.address, peer.identity.listen_port, list(self._pending_paths))
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
                self.status.setText(f"发送完成，失败：{'、'.join(failures)}")
            elif failed:
                self.status.setText(f"发送到 {peer_name} 失败。")
            else:
                self.status.setText("发送完成。")
            self._update_send_button()

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
                device_id=self._config.device_id,
            )
            save_config(config)
            self._config = config
            set_start_on_boot(
                config.start_on_boot,
                _current_executable(),
                app_name="LocalNetFTP",
            )
            self._restart_transfer_server()
            self._restart_discovery()
            QMessageBox.information(self, "LocalNetFTP", "设置已保存。")

        def _start_transfer_server(self) -> None:
            self._transfer_server = TransferServer(self._config.receive_dir, TRANSFER_LISTEN_PORT)
            try:
                self._transfer_server.start()
            except OSError as exc:
                self._transfer_server = None
                self.status.setText(f"文件接收服务启动失败：{exc}")

        def _restart_transfer_server(self) -> None:
            self._stop_transfer_server()
            self._start_transfer_server()

        def _stop_transfer_server(self) -> None:
            if self._transfer_server is not None:
                self._transfer_server.stop()
                self._transfer_server = None

        def _start_discovery(self) -> None:
            identity = create_device_identity(
                self._config.device_name,
                listen_port=TRANSFER_LISTEN_PORT,
                device_id=self._config.device_id,
            )
            self._discovery_service = DiscoveryService(identity)
            try:
                self._discovery_service.start()
            except OSError as exc:
                self._discovery_service = None
                self.status.setText(f"局域网发现启动失败：{exc}")
                return
            self.status.setText("正在发现局域网用户。拖入文件或文件夹后会显示在待发送列表。")

        def _restart_discovery(self) -> None:
            self._stop_discovery()
            self._start_discovery()

        def _stop_discovery(self) -> None:
            if self._discovery_service is not None:
                self._discovery_service.stop()
                self._discovery_service = None

        def _refresh_peers(self) -> None:
            selected_names = set(self._selected_peer_names())
            self.peer_list.clear()
            self._peers_by_row = {}
            if self._discovery_service is None:
                self._update_send_button()
                return

            peers = self._discovery_service.peers()
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
            if QSystemTrayIcon.isSystemTrayAvailable():
                event.ignore()
                self.hide()
            else:
                event.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = FloatingWindow()
    icon = app.style().standardIcon(QStyle.SP_DriveNetIcon)
    window.setWindowIcon(icon)

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("LocalNetFTP")

    menu = QMenu()
    show_action = QAction("显示", menu)
    quit_action = QAction("退出", menu)
    menu.addAction(show_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    def show_window() -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    show_action.triggered.connect(show_window)
    quit_action.triggered.connect(app.quit)

    def on_tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if window.isVisible():
                window.hide()
            else:
                show_window()

    tray.activated.connect(on_tray_activated)
    tray.show()
    app.aboutToQuit.connect(window._stop_discovery)
    app.aboutToQuit.connect(window._stop_transfer_server)
    show_window()

    return app.exec()


def _current_executable() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.argv[0]).resolve()
