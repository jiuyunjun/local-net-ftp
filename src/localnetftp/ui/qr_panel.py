from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)


class QrPanel(QWidget):
    """Shared, non-scrolling QR picker for mobile upload and download."""

    def __init__(self, preference, remember, pixmap_factory):
        super().__init__()
        self._preference = preference
        self._remember = remember
        self._pixmap_factory = pixmap_factory
        self.interface = QComboBox()
        self.interface.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.interface.setMinimumContentsLength(20)
        self.interface.setEnabled(False)
        self.show_qr = QPushButton("显示二维码")
        self.show_qr.setObjectName("primaryButton")
        self.show_qr.setEnabled(False)
        self.show_qr.clicked.connect(self.show_selected)
        self.interface.currentIndexChanged.connect(self._selection_changed)
        row = QHBoxLayout()
        row.addWidget(self.interface, 1)
        row.addWidget(self.show_qr)

        self.card = QWidget()
        self.card.setObjectName("shareAddressCard")
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setMinimumSize(198, 198)
        self.name = QLabel("正在加载网卡…")
        self.name.setWordWrap(True)
        self.name.setTextFormat(Qt.PlainText)
        self.name.setAlignment(Qt.AlignCenter)
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.link = QLabel()
        self.link.setTextFormat(Qt.PlainText)
        self.link.setAlignment(Qt.AlignCenter)
        self.link.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy = QPushButton("复制网址")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(self._copy_url)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(12, 12, 12, 12)
        card_layout.addWidget(self.qr, alignment=Qt.AlignCenter)
        card_layout.addWidget(self.name)
        card_layout.addWidget(self.link)
        card_layout.addWidget(self.copy, alignment=Qt.AlignCenter)
        self.status = QLabel("正在加载网卡…")
        self.status.setObjectName("statusLabel")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(QLabel("局域网网卡（手机与电脑需在同一网络）"))
        layout.addLayout(row)
        layout.addWidget(self.card)
        layout.addWidget(self.status)

    def set_urls(self, urls):
        self.interface.blockSignals(True)
        self.interface.clear()
        for address in urls:
            self.interface.addItem(f"{address.address} · {address.interface_name}", address)
        self.interface.blockSignals(False)
        self.interface.setEnabled(bool(urls))
        self.show_qr.setEnabled(bool(urls))
        self.apply_preference(*self._preference())
        self._clear_qr()
        if not urls:
            self.status.setText("未找到可用网卡，请连接网络后重新打开窗口")

    def apply_preference(self, name, address):
        choices = [self.interface.itemData(i) for i in range(self.interface.count())]
        index = next((i for i, item in enumerate(choices) if item.interface_name == name and item.address == address), None)
        if index is None:
            index = next((i for i, item in enumerate(choices) if name and item.interface_name == name), None)
        if index is None:
            index = next((i for i, item in enumerate(choices) if address and item.address == address), 0)
        if choices and self.interface.currentIndex() != index:
            self.interface.blockSignals(True)
            self.interface.setCurrentIndex(index)
            self.interface.blockSignals(False)
            self._clear_qr()

    def _clear_qr(self):
        self.qr.clear()
        self.qr.setText("选择网卡后显示二维码")
        self.name.setText(self.interface.currentText() or "暂无可用网卡")
        self.interface.setToolTip(self.interface.currentText())
        self.link.clear()
        self.copy.setEnabled(False)
        self.status.setText("已恢复默认网卡，可切换后点击显示二维码")

    def _selection_changed(self, *_args):
        self._clear_qr()
        self._save_selection()

    def _save_selection(self):
        address = self.interface.currentData()
        if address is None:
            return
        try:
            self._remember(address.interface_name, address.address)
        except OSError:
            self.status.setText("网卡可正常使用，但无法保存偏好，请检查配置目录权限")

    def show_selected(self):
        address = self.interface.currentData()
        if address is None:
            return
        self.status.setText("手机扫码访问；若无法打开，请检查 Wi-Fi 和防火墙")
        self._save_selection()
        pixmap = self._pixmap_factory(address.url, 6)
        self.qr.setFixedSize(max(198, pixmap.width()), max(198, pixmap.height()))
        self.qr.setPixmap(pixmap)
        self.name.setText(address.interface_name)
        self.link.setText(address.url)
        self.copy.setEnabled(True)
        self.window().layout().activate()
        self.window().adjustSize()

    def _copy_url(self):
        QApplication.clipboard().setText(self.link.text())
        self.status.setText("已复制网址")
