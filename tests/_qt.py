"""Bounded waits and window-system input for real Qt test surfaces."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

import shiboken6

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QSize, Qt, QUrl
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QWindow
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget


def wait_until(
    predicate: Callable[[], bool],
    *,
    description: str | Callable[[], str],
    timeout_ms: int = 3000,
) -> None:
    deadline = monotonic() + timeout_ms / 1000
    while True:
        QCoreApplication.processEvents()
        if predicate():
            return
        if monotonic() >= deadline:
            detail = description() if callable(description) else description
            raise AssertionError(f"Timed out waiting for {detail}")
        QTest.qWait(5)


def wait_for_geometry(widget: QWidget, *, size: QSize) -> None:
    def matches() -> bool:
        if widget.size() != size:
            return False
        window_widget = widget.window()
        window = window_widget.windowHandle()
        if window is None or window.size() != window_widget.size():
            return False
        if isinstance(widget, QQuickWidget):
            root = widget.rootObject()
            return root is not None and (root.width(), root.height()) == (
                size.width(),
                size.height(),
            )
        return True

    def diagnostic() -> str:
        window = widget.window().windowHandle()
        root = widget.rootObject() if isinstance(widget, QQuickWidget) else None
        return (
            f"{type(widget).__name__} geometry {size}: widget={widget.size()}, "
            f"window={window.size() if window else None}, "
            f"root={(root.width(), root.height()) if root else None}, "
            f"minimum={widget.minimumSize()}, maximum={widget.maximumSize()}, "
            f"screen={widget.screen().availableGeometry()}, flags={widget.windowFlags()}"
        )

    wait_until(matches, description=diagnostic)


def show_and_activate(widget: QWidget, *, size: QSize | None = None) -> None:
    if size is not None:
        widget.resize(size)
    widget.show()
    window = widget.window()
    native_window = window.windowHandle()
    assert native_window is not None, "Visible widget has no native window"
    assert QTest.qWaitForWindowExposed(native_window, 3000), "Window was not exposed"
    window.raise_()
    window.activateWindow()
    assert QTest.qWaitForWindowActive(native_window, 3000), "Window was not activated"
    if size is not None:
        # Native backends may constrain the initial size on a small screen.
        widget.resize(size)
        wait_for_geometry(widget, size=size)


def _native_host(widget: QWidget) -> QWidget:
    # Native children (for example Cocoa CALayer-backed toolbars) receive input
    # through their own QWindow. Alien widgets share their nearest native host.
    native_host = widget
    while native_host.windowHandle() is None and native_host.parentWidget() is not None:
        native_host = native_host.parentWidget()
    return native_host


def _window_position(widget: QWidget, pos: QPoint) -> tuple[QWindow, QPoint]:
    window = _native_host(widget).windowHandle()
    assert window is not None and window.isExposed(), "Input requires an exposed window"
    position = window.mapFromGlobal(widget.mapToGlobal(pos))
    assert window.geometry().size().width() > position.x() >= 0
    assert window.geometry().size().height() > position.y() >= 0
    return window, position


def _move_cocoa_cursor(widget: QWidget, global_position: QPoint) -> None:
    # QCursor.setPos is a no-op at the current position. It cannot establish a
    # new crossing there; callers qualifying input must choose distinct points.
    if QCursor.pos() == global_position:
        return

    class Delivery(QObject):
        received = False
        last_event = None

        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            if (
                event.type() not in (QEvent.Type.Enter, QEvent.Type.MouseMove)
                or not shiboken6.isValid(widget)
            ):
                return False
            native_host = _native_host(widget)
            if watched is widget or watched is native_host:
                # Input helpers address integer logical pixels, including when
                # native coordinates are fractional on a scaled display.
                local = event.position().toPoint()
                global_point = event.globalPosition().toPoint()
                self.last_event = (type(watched).__name__, event.type().name, local, global_point)
                # Qt can combine a native Leave/Enter into an Enter delivered to
                # the native parent's widget alone. A subsequent move routes to
                # its alien child. Acknowledge transport, independently of hover.
                if (
                    global_point == global_position
                    and local == watched.mapFromGlobal(global_position)
                ):
                    self.received = True
            return False

    delivery = Delivery()
    app = QCoreApplication.instance()
    assert app is not None
    app.installEventFilter(delivery)

    def delivered() -> bool:
        assert shiboken6.isValid(widget), "Pointer target was destroyed during input delivery"
        return delivery.received and QCursor.pos() == global_position

    try:
        # Install before posting: synchronous delivery must also be observed.
        # Cocoa posts a CGEvent; its system position can advance before AppKit
        # and Qt dispatch the corresponding input. Do not add a second Qt move.
        QCursor.setPos(global_position)
        QGuiApplication.sync()
        wait_until(
            delivered,
            description=lambda: (
                f"pointer delivery for {type(widget).__name__} at {global_position}: "
                f"cursor={QCursor.pos()}, last_target_event={delivery.last_event}"
            ),
        )
    finally:
        app.removeEventFilter(delivery)


def mouse_move(widget: QWidget, pos: QPoint, *, delay: int = -1, sync_cursor: bool = False) -> None:
    """Move through the input backend; Cocoa warps await positioned input delivery.

    A native-parent crossing can precede delivery to its alien child. Callers
    qualifying a control still observe that control's hover or click separately.

    A Cocoa request at the current system position is a no-op, not a fresh input.
    """
    if sync_cursor:
        _window_position(widget, pos)
        cocoa = QGuiApplication.platformName() == "cocoa"
        if cocoa:
            assert QGuiApplication.mouseButtons() == Qt.MouseButton.NoButton, (
                "Native cursor movement requires a released pointer; use mouse_drag for dragging"
            )
        global_position = widget.mapToGlobal(pos)
        if cocoa:
            _move_cocoa_cursor(widget, global_position)
            if delay > 0:
                QTest.qWait(delay)
            return
        QCursor.setPos(global_position)
        # Flush available events and observe the system position. Native event
        # delivery remains asynchronous; callers wait for the control's result.
        QGuiApplication.sync()
        wait_until(
            lambda: QCursor.pos() == global_position,
            description=lambda: (
                f"global pointer position {global_position}: actual={QCursor.pos()}, "
                f"screen={widget.screen().geometry()}, widget={widget.geometry()}"
            ),
        )
        if delay > 0:
            QTest.qWait(delay)
        # Sync and delay process callbacks which can recreate the native host.
        window, position = _window_position(widget, widget.mapFromGlobal(global_position))
        # The warp can update QGuiApplication's last global position through a
        # different native surface. A subsequent QTest QPA move at that same
        # position is discarded before QWidgetWindow can route it to our host.
        # Deliver the move through the target window's widget dispatcher.
        event = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(position), QPointF(position),
            QPointF(global_position), Qt.MouseButton.NoButton,
            QGuiApplication.mouseButtons(), QGuiApplication.keyboardModifiers(),
        )
        QCoreApplication.sendEvent(window, event)
        QCoreApplication.processEvents()
    else:
        window, position = _window_position(widget, pos)
        QTest.mouseMove(window, position, delay)


def mouse_press(widget: QWidget, button: Qt.MouseButton, *, pos: QPoint, delay: int = -1) -> None:
    window, position = _window_position(widget, pos)
    QTest.mousePress(window, button, pos=position, delay=delay)


def mouse_release(widget: QWidget, button: Qt.MouseButton, *, pos: QPoint, delay: int = -1) -> None:
    window, position = _window_position(widget, pos)
    QTest.mouseRelease(window, button, pos=position, delay=delay)


def mouse_click(widget: QWidget, button: Qt.MouseButton, *, pos: QPoint, delay: int = -1) -> None:
    window, position = _window_position(widget, pos)
    QTest.mouseClick(window, button, pos=position, delay=delay)


def mouse_drag(
    widget: QWidget, start: QPoint, end: QPoint, *, steps: int = 6, delay: int = 12
) -> None:
    assert steps > 0
    position = start
    mouse_press(widget, Qt.MouseButton.LeftButton, pos=start)
    try:
        for step in range(1, steps + 1):
            next_position = start + (end - start) * (step / steps)
            mouse_move(widget, next_position, delay=delay)
            position = next_position
    finally:
        mouse_release(widget, Qt.MouseButton.LeftButton, pos=position)


def key_click(
    widget: QWidget, key: Qt.Key, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> None:
    window = widget.window().windowHandle()
    assert window is not None and window.isActive(), "Keys require an active window"
    QTest.keyClick(window, key, modifiers)


def dispose_qobject(obj: QObject) -> None:
    if not shiboken6.isValid(obj):
        return
    obj.deleteLater()
    QCoreApplication.sendPostedEvents(obj, QEvent.Type.DeferredDelete)


def dispose_widget(widget: QWidget) -> None:
    if not shiboken6.isValid(widget):
        return
    try:
        try:
            widget.close()
        finally:
            qml_widgets = []
            if shiboken6.isValid(widget):
                qml_widgets = (
                    [widget]
                    if isinstance(widget, QQuickWidget)
                    else widget.findChildren(QQuickWidget)
                )
            for qml_widget in qml_widgets:
                # Destroy QML while its host and context objects are still valid.
                if shiboken6.isValid(qml_widget):
                    qml_widget.setSource(QUrl())
    finally:
        dispose_qobject(widget)
