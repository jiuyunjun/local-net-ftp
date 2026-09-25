import pytest
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget
from PySide6.QtGui import QPixmap

from localnetftp.share import ShareAddress
from localnetftp.ui.qr_panel import QrPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def addresses():
    return [ShareAddress("Ethernet", "10.0.0.2", "http://10.0.0.2:49302/"),
            ShareAddress("Wi-Fi", "192.168.1.3", "http://192.168.1.3:49302/")]


def pixmap(*_args):
    result = QPixmap(222, 222)
    result.fill()
    return result


def test_restore_interface_after_ip_change_and_fallback(app):
    panel = QrPanel(lambda: ("Wi-Fi", "192.168.1.2"), lambda *args: None, pixmap)
    panel.set_urls(addresses())
    assert panel.interface.currentIndex() == 1
    panel.set_urls(addresses()[:1])
    assert panel.interface.currentIndex() == 0
    panel.set_urls([])
    assert not panel.show_qr.isEnabled()
    panel.close()


def test_shared_choice_and_qr_fits_without_scroll(app):
    preference = ["", ""]
    panels = []
    def remember(name, address):
        preference[:] = [name, address]
        for item in panels:
            item.apply_preference(name, address)
    windows = []
    for _ in range(2):
        window = QWidget()
        window.setMinimumWidth(520)
        panel = QrPanel(lambda: tuple(preference), remember, pixmap)
        QVBoxLayout(window).addWidget(panel)
        panel.set_urls(addresses())
        panels.append(panel)
        windows.append(window)
    panels[0].interface.setCurrentIndex(1)
    assert panels[1].interface.currentIndex() == 1
    assert preference == ["Wi-Fi", "192.168.1.3"]
    for window, panel in zip(windows, panels):
        window.show()
        panel.show_selected()
        app.processEvents()
        assert panel.qr.width() >= panel.qr.pixmap().width()
        assert panel.qr.height() >= panel.qr.pixmap().height()
        assert not window.findChildren(QScrollArea)
        rect = panel.qr.rect()
        assert window.rect().contains(panel.qr.mapTo(window, rect.topLeft()))
        assert window.rect().contains(panel.qr.mapTo(window, rect.bottomRight()))
        window.close()
