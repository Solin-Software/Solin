from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from solin.projection.window import FloatingPreviewWindow, ProjectionWindow


_APP = QApplication.instance() or QApplication([])


class _FontManager(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        pass

    def family(self, _name: str) -> str:
        return "Arial"


def _projection_window() -> ProjectionWindow:
    screen = QApplication.primaryScreen()
    assert screen is not None
    return ProjectionWindow(screen, 0, _FontManager())


def _close(window) -> None:
    window.close()
    window.deleteLater()
    _APP.processEvents()


def test_projection_window_hides_the_pointer_by_default() -> None:
    """The audience should never see the operator's pointer on the projection."""
    window = _projection_window()
    try:
        _APP.processEvents()
        assert window.cursor().shape() == Qt.CursorShape.BlankCursor
    finally:
        _close(window)


def test_projection_window_hides_the_pointer_over_native_video() -> None:
    """libobs draws media into a native child window, which must be blanked too."""
    window = _projection_window()
    try:
        window.set_native_output_active(True)
        _APP.processEvents()
        surface = window.native_video_surface
        assert surface.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        assert surface.cursor().shape() == Qt.CursorShape.BlankCursor
    finally:
        _close(window)


def test_projection_window_keeps_the_pointer_hidden_after_refitting() -> None:
    """A resolution change cycles the window through normal and fullscreen."""
    window = _projection_window()
    try:
        window.showNormal()
        window.resize(320, 180)
        window.refit_to_screen()
        _APP.processEvents()
        assert window.cursor().shape() == Qt.CursorShape.BlankCursor
        assert window.native_video_surface.cursor().shape() == Qt.CursorShape.BlankCursor
    finally:
        _close(window)


def test_floating_preview_keeps_its_pointer() -> None:
    """The operator resizes and drags the preview, so it needs a visible pointer."""
    window = FloatingPreviewWindow(_FontManager())
    try:
        _APP.processEvents()
        assert window.cursor().shape() != Qt.CursorShape.BlankCursor
    finally:
        _close(window)
