from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget

from solin.widgets.browser.crop_overlay import CropOverlay


_APP = QApplication.instance() or QApplication([])


def _process_events() -> None:
    _APP.processEvents()


def test_crop_overlay_tracks_target_when_top_level_window_moves():
    window = QWidget()
    window.setGeometry(80, 90, 640, 480)
    container = QWidget(window)
    container.setGeometry(20, 30, 500, 360)
    target = QWidget(container)
    target.setGeometry(15, 25, 320, 180)
    window.show()
    _process_events()

    overlay = CropOverlay(target)
    _process_events()
    initial_position = overlay.pos()
    initial_size = overlay.size()

    window.move(window.pos() + QPoint(140, 75))
    _process_events()

    assert overlay.pos() == target.mapToGlobal(target.rect().topLeft())
    assert overlay.pos() == initial_position + QPoint(140, 75)
    assert overlay.size() == initial_size

    overlay.close()
    window.close()
    _process_events()


def test_crop_overlay_tracks_target_when_intermediate_parent_moves():
    window = QWidget()
    window.setGeometry(80, 90, 640, 480)
    container = QWidget(window)
    container.setGeometry(20, 30, 500, 360)
    target = QWidget(container)
    target.setGeometry(15, 25, 320, 180)
    window.show()
    _process_events()

    overlay = CropOverlay(target)
    _process_events()

    container.move(container.pos() + QPoint(35, 45))
    _process_events()

    assert overlay.pos() == target.mapToGlobal(target.rect().topLeft())

    overlay.close()
    window.close()
    _process_events()
