from __future__ import annotations

import sys
from pathlib import Path

from localnetftp.config import (
    AppConfig,
    is_start_on_boot_enabled,
    load_config,
    save_config,
    set_start_on_boot,
)


def run_tray_app() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
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
            self.setMinimumSize(420, 220)
            self.setAcceptDrops(True)

            self._config = load_config()

            title = QLabel("LocalNetFTP")
            title.setObjectName("titleLabel")

            status = QLabel("局域网传输功能开发中。现在可以先配置接收目录和开机自启动。")
            status.setWordWrap(True)

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

            save_button = QPushButton("保存设置")
            save_button.clicked.connect(self._save)

            layout = QVBoxLayout(self)
            layout.addWidget(title)
            layout.addWidget(status)
            layout.addSpacing(8)
            layout.addWidget(receive_label)
            layout.addLayout(receive_layout)
            layout.addWidget(self.start_on_boot)
            layout.addStretch(1)
            layout.addWidget(save_button, alignment=Qt.AlignRight)

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
                QPushButton {
                    min-height: 30px;
                    padding: 3px 14px;
                }
                """
            )

        def _choose_receive_dir(self) -> None:
            selected = QFileDialog.getExistingDirectory(self, "选择接收目录", self.receive_dir.text())
            if selected:
                self.receive_dir.setText(selected)

        def _save(self) -> None:
            receive_dir = Path(self.receive_dir.text()).expanduser()
            config = AppConfig(receive_dir=receive_dir, start_on_boot=self.start_on_boot.isChecked())
            save_config(config)
            set_start_on_boot(
                config.start_on_boot,
                _current_executable(),
                app_name="LocalNetFTP",
            )
            QMessageBox.information(self, "LocalNetFTP", "设置已保存。")

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
    show_window()

    return app.exec()


def _current_executable() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.argv[0]).resolve()
