from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QTimer, Slot, Qt
from PySide6.QtGui import QColor, QPixmap, QWheelEvent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.timer_engine import TimerEngine
from solin.core.timer.models import ClockConfig
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.playlist.visuals import PlaylistThumbnailProvider
from solin.ui.qml.timer_output import ClockRenderBridge


_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip(
        "QML widget host tests require QApplication before QCoreApplication.",
        allow_module_level=True,
    )


class _CursorProbe(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.exited = 0

    @Slot()
    def pointerEnter(self) -> None:  # noqa: N802
        self.entered += 1

    @Slot()
    def pointerExit(self) -> None:  # noqa: N802
        self.exited += 1


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    texts: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        texts.append(text)
    for child in item.childItems():
        texts.extend(_visible_texts(child, parent_visible=visible))
    return texts


def test_qml_host_renders_clock_face_content() -> None:
    engine = TimerEngine()
    bridge = ClockRenderBridge(engine, lambda: ClockConfig())
    widget = QQuickWidget()
    widget.resize(800, 450)

    configure_qml_host(
        widget,
        type_name="ClockFace",
        clear_color="#000000",
        context_properties={
            "clock": bridge,
            "timerDigitFontFamily": "Arial",
        },
    )
    widget.show()

    QTimer.singleShot(50, _APP.quit)
    _APP.exec()

    root = widget.rootObject()
    assert widget.errors() == []
    assert root is not None
    assert root.width() == 800
    assert root.height() == 450
    assert any(":" in text for text in _visible_texts(root))


def test_timer_pointer_area_enters_and_exits_native_cursor_state() -> None:
    probe = _CursorProbe()
    widget = QQuickWidget()
    widget.resize(160, 48)

    configure_qml_host(
        widget,
        type_name="TimerButton",
        clear_color="#000000",
        context_properties={"timer": probe},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("text", "Start")
    widget.show()
    _APP.processEvents()

    QTest.mouseMove(widget, QPoint(12, 12))
    QTest.qWait(30)

    assert probe.entered == 1
    assert probe.exited == 0

    root.setProperty("enabled", False)
    QTest.qWait(30)

    assert probe.entered == 1
    assert probe.exited == 1


def _send_thumbnail_wheel(widget: QQuickWidget, modifiers) -> None:
    position = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        position,
        widget.mapToGlobal(position.toPoint()).toPointF(),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget.quickWindow(), event)


def test_image_framing_thumbnail_handles_click_zoom_pan_and_reset() -> None:
    portrait = QPixmap(100, 200)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
        image_providers={
            "playlistthumbs": PlaylistThumbnailProvider({"portrait": portrait}),
        },
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("imageSource", "image://playlistthumbs/portrait/0")
    root.setProperty("projectionAspectRatio", 16 / 9)
    widget.show()
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True

    clicked = QSignalSpy(root.clicked)
    edited = QSignalSpy(root.framingEdited)
    reset = QSignalSpy(root.framingReset)

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(53, 28), delay=5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(53, 28))
    assert clicked.count() == 1
    assert edited.count() == 0

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(50, 44), delay=5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 44))
    assert clicked.count() == 1
    assert edited.count() == 0
    assert root.property("framingActive") is False
    assert root.property("panAvailable") is False

    _send_thumbnail_wheel(widget, Qt.KeyboardModifier.NoModifier)
    assert root.property("framingZoom") == 1.0
    assert edited.count() == 0

    _send_thumbnail_wheel(widget, Qt.KeyboardModifier.ControlModifier)
    assert root.property("framingZoom") > 1.0
    assert edited.count() == 1
    assert root.property("framingActive") is True
    assert root.property("panAvailable") is True

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(50, 80), delay=5)
    assert root.property("panning") is True
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
    assert clicked.count() == 1
    assert edited.count() > 1
    assert root.property("panning") is False

    cover_zoom = float(root.coverZoom())
    for _step in range(20):
        if float(root.property("framingZoom")) >= cover_zoom:
            break
        _send_thumbnail_wheel(widget, Qt.KeyboardModifier.ControlModifier)
    assert root.property("framingZoom") == pytest.approx(cover_zoom)

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(86, 42))
    assert reset.count() == 1
    assert root.property("framingActive") is False
