from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

from solin.ui import helpers

_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip(
        "UI helper tests require QApplication before QCoreApplication.",
        allow_module_level=True,
    )


class _CursorTarget:
    def __init__(self):
        self.set_values = []
        self.unset_count = 0

    def setCursor(self, cursor):
        self.set_values.append(cursor)

    def unsetCursor(self):
        self.unset_count += 1


class _HostWidget:
    def __init__(self, window):
        self._window = window

    def windowHandle(self):
        return self._window


class _QmlWidget(_CursorTarget):
    def __init__(self):
        super().__init__()
        self.quick_window = _CursorTarget()
        self.host_window = _CursorTarget()
        self.host_widget = _HostWidget(self.host_window)

    def quickWindow(self):
        return self.quick_window

    def window(self):
        return self.host_widget


def _wait_for(predicate, timeout_ms: int = 1000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(10)
    return bool(predicate())


def test_qml_pointer_cursor_applies_to_widget_quick_window_and_host_window():
    qml_widget = _QmlWidget()

    helpers.begin_qml_pointer_cursor(qml_widget)

    assert len(qml_widget.set_values) == 1
    assert len(qml_widget.quick_window.set_values) == 1
    assert len(qml_widget.host_window.set_values) == 1


def test_qml_pointer_cursor_unsets_all_cursor_targets():
    qml_widget = _QmlWidget()

    helpers.end_qml_pointer_cursor(qml_widget)

    assert qml_widget.unset_count == 1
    assert qml_widget.quick_window.unset_count == 1
    assert qml_widget.host_window.unset_count == 1


def test_qml_pointer_cursor_preserves_the_requested_shape():
    qml_widget = _QmlWidget()

    helpers.set_qml_pointer_cursor(qml_widget, Qt.CursorShape.OpenHandCursor)

    assert qml_widget.set_values == [Qt.CursorShape.OpenHandCursor]
    assert qml_widget.quick_window.set_values == [Qt.CursorShape.OpenHandCursor]
    assert qml_widget.host_window.set_values == [Qt.CursorShape.OpenHandCursor]


def test_qml_pointer_cursor_state_restores_nested_cursor_shapes():
    qml_widget = _QmlWidget()
    state = helpers.QmlPointerCursorState(qml_widget)

    state.enter()
    state.enter_shaped("pan", Qt.CursorShape.OpenHandCursor.value)
    state.enter_shaped("reset", Qt.CursorShape.PointingHandCursor.value)
    state.exit_shaped("reset")
    state.update_shaped("pan", Qt.CursorShape.ClosedHandCursor.value)
    state.exit_shaped("pan")

    assert qml_widget.set_values == [
        Qt.CursorShape.PointingHandCursor,
        Qt.CursorShape.OpenHandCursor,
        Qt.CursorShape.PointingHandCursor,
        Qt.CursorShape.OpenHandCursor,
        Qt.CursorShape.ClosedHandCursor,
        Qt.CursorShape.PointingHandCursor,
    ]

    state.exit()
    assert qml_widget.unset_count == 1


def test_qml_pointer_cursor_state_handles_out_of_order_sibling_exit():
    qml_widget = _QmlWidget()
    state = helpers.QmlPointerCursorState(qml_widget)

    state.enter_shaped("pan", Qt.CursorShape.OpenHandCursor.value)
    state.enter_shaped("reset", Qt.CursorShape.PointingHandCursor.value)
    state.exit_shaped("pan")

    assert qml_widget.set_values[-1] == Qt.CursorShape.PointingHandCursor

    state.exit_shaped("reset")
    assert qml_widget.unset_count == 1


def test_fade_in_removes_opacity_effect_after_animation():
    widget = QWidget()
    try:
        helpers.fade_in(widget, duration=1)

        assert isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect)

        assert _wait_for(lambda: widget.graphicsEffect() is None)
    finally:
        widget.deleteLater()


def test_fade_in_safely_replaces_an_active_animation():
    widget = QWidget()
    try:
        helpers.fade_in(widget, duration=1000)
        first_effect = widget.graphicsEffect()
        assert isinstance(first_effect, QGraphicsOpacityEffect)

        helpers.fade_in(widget, duration=1)
        second_effect = widget.graphicsEffect()
        assert isinstance(second_effect, QGraphicsOpacityEffect)
        assert second_effect is not first_effect

        assert _wait_for(lambda: widget.graphicsEffect() is None)
    finally:
        widget.deleteLater()
