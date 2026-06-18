from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPoint, QTimer, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.timer_engine import TimerEngine
from solin.core.timer.models import ClockConfig
from solin.ui.qml.host import configure_qml_host
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
