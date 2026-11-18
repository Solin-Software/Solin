"""The right-hand dock (Live meeting console) is resized by dragging its left edge."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from solin.widgets.side_dock import ResizableSideDock


def _window(dock_width: int = 340):
    window = QWidget()
    window.resize(1200, 700)
    content = QWidget(window)               # like the main window: the dock lives in an inner row
    QHBoxLayout(window).addWidget(content)
    window.layout().setContentsMargins(0, 0, 0, 0)
    row = QHBoxLayout(content)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QWidget(content), 1)
    dock = ResizableSideDock(dock_width, parent=window)
    row.addWidget(dock)
    dock.layout().addWidget(QLabel("panel", dock))   # the console is added after the dock exists
    window.show()
    QTest.qWaitForWindowExposed(window)
    return window, dock


def _drag(dock: ResizableSideDock, dx: int) -> None:
    grip = dock.findChild(QWidget, "SideDockGrip")
    start = QPoint(2, 50)
    QTest.mousePress(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(grip, start + QPoint(dx, 0))
    QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(dx, 0))


def test_dragging_the_left_edge_resizes_and_reports_the_width():
    window, dock = _window()
    try:
        committed: list[int] = []
        dock.width_committed.connect(committed.append)
        grip = dock.findChild(QWidget, "SideDockGrip")
        assert grip.isVisible() and grip.cursor().shape() == Qt.CursorShape.SizeHorCursor
        assert grip.geometry().height() == dock.height()
        # The panel (often a native window container, which paints over siblings) starts past the grip.
        panel = dock.layout().itemAt(0).widget()
        assert not panel.geometry().intersects(grip.geometry())
        _drag(dock, -100)                      # left: wider
        assert dock.width() == 440 and committed == [440]
        _drag(dock, 400)                       # right, past the minimum
        assert dock.width() == 280
        _drag(dock, -2000)                     # left, past the maximum (60% of the window)
        assert dock.width() == 720
    finally:
        window.close()


def test_a_saved_width_is_clamped_to_the_window():
    window, dock = _window(5000)
    try:
        assert dock.width() == 720
        window.resize(800, 700)
        QTest.qWait(50)
        assert dock.width() == 480
    finally:
        window.close()
