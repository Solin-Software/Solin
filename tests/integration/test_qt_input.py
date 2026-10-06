"""Input transport contracts, independent of a control's hover response."""

from __future__ import annotations

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QTimer, Qt
from PySide6.QtGui import QCursor, QEnterEvent, QGuiApplication, QMouseEvent, QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tests import _qt


def _posted_cursor(monkeypatch, initial, on_post):
    class PostedCursor:
        current = initial
        posts = []

        @classmethod
        def pos(cls):
            return cls.current

        @classmethod
        def setPos(cls, value):  # noqa: N802 - QCursor API
            if value != cls.current:
                cls.current = value
                cls.posts.append(value)
                on_post()

    monkeypatch.setattr(_qt, "QCursor", PostedCursor)
    monkeypatch.setattr(QGuiApplication, "platformName", lambda: "cocoa")
    return PostedCursor


@pytest.fixture(params=["window", "ancestor", "widget"])
def input_surface(request):
    initial_cursor = QCursor.pos()
    host = QWidget()
    host.resize(320, 200)
    container = QWidget(host)
    container.setGeometry(20, 20, 260, 160)
    widget = QWidget(container)
    widget.setGeometry(20, 20, 200, 120)
    if request.param == "ancestor":
        container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    elif request.param == "widget":
        widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    try:
        _qt.show_and_activate(host)
        window, position = _qt._window_position(widget, QPoint(10, 10))
        QTest.mouseMove(window, position, 0)
        yield host, widget
    finally:
        _qt.dispose_widget(host)
        QCursor.setPos(initial_cursor)
        QGuiApplication.sync()


@pytest.mark.parametrize("delivery_delay_ms", [0, 80], ids=["immediate", "delayed"])
def test_cursor_move_waits_for_positioned_input_delivery(
    input_surface, monkeypatch, delivery_delay_ms,
):
    host, widget = input_surface
    point = QPoint(80, 50)
    window, position = _qt._window_position(widget, point)
    expected = widget.mapToGlobal(point)
    received = []

    class Recorder(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            if watched is widget and event.type() == QEvent.Type.MouseMove:
                received.append(event.globalPosition().toPoint())
            return False

    recorder = Recorder()
    app = QApplication.instance()
    app.installEventFilter(recorder)
    timer = QTimer(host)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: QTest.mouseMove(window, position, 0))

    # Model Cocoa's system position advancing before the posted input reaches
    # Qt. Delivery still traverses the real QPA/QWindow widget dispatcher.
    def post():
        if delivery_delay_ms:
            timer.start(delivery_delay_ms)
        else:
            QTest.mouseMove(window, position, 0)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert cursor.pos() == expected
        assert expected in received, "Cursor position was acknowledged before input delivery"
        assert cursor.posts == [expected], "Input must be posted exactly once"
    finally:
        timer.stop()
        app.removeEventFilter(recorder)


@pytest.mark.parametrize("input_surface", ["window"], indirect=True)
def test_cursor_move_ignores_other_receivers_and_coordinates(input_surface, monkeypatch):
    host, widget = input_surface
    point = QPoint(80, 50)
    window, position = _qt._window_position(widget, point)
    expected = widget.mapToGlobal(point)
    other_window = QWindow()
    other_widget = QWidget()
    timer = QTimer(host)
    timer.setSingleShot(True)
    delivered = []

    def deliver():
        QTest.mouseMove(window, position, 0)
        delivered.append(True)

    timer.timeout.connect(deliver)

    def post():
        # Positional input on another receiver is not acknowledgement, even at
        # the requested global point. Neither is a mis-mapped target event.
        for receiver, local, global_point in (
            (other_window, position, expected),
            (other_widget, other_widget.mapFromGlobal(expected), expected),
            (host, host.mapFromGlobal(expected) + QPoint(0, 1), expected),
            (widget, point, expected + QPoint(0, 1)),
            (widget, point + QPoint(0, 1), expected),
        ):
            event = QMouseEvent(
                QEvent.Type.MouseMove, QPointF(local), QPointF(local), QPointF(global_point),
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            )
            QCoreApplication.sendEvent(receiver, event)
        timer.start(80)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert delivered, "Unrelated or mis-mapped input must not acknowledge delivery"
        assert cursor.posts == [expected]
    finally:
        timer.stop()
        _qt.dispose_qobject(other_window)
        _qt.dispose_widget(other_widget)


@pytest.mark.parametrize("input_surface", ["window"], indirect=True)
def test_cursor_move_accepts_target_widget_entry(input_surface, monkeypatch):
    host, widget = input_surface
    point = QPoint(80, 50)
    window, position = _qt._window_position(widget, point)
    expected = widget.mapToGlobal(point)
    entries = []

    class Recorder(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            if watched is widget and event.type() == QEvent.Type.Enter:
                entries.append((event.position(), event.scenePosition(), event.globalPosition()))
            return False

    recorder = Recorder()
    app = QApplication.instance()
    app.installEventFilter(recorder)

    def post():
        # QWidgetWindow selects the receiver, then dispatchEnterLeave rebuilds
        # its local position from global coordinates. The QWidget scene position
        # still belongs to the host window, which differs from the Quick scene.
        window_point = QPointF(position + QPoint(7, 9))
        event = QEnterEvent(window_point, window_point, QPointF(expected))
        QCoreApplication.sendEvent(window, event)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert entries == [(QPointF(point), QPointF(host.mapFromGlobal(expected)), QPointF(expected))]
        assert cursor.posts == [expected]
    finally:
        app.removeEventFilter(recorder)


@pytest.mark.parametrize("input_surface", ["ancestor"], indirect=True)
def test_cursor_move_accepts_native_parent_crossing_before_child_move(input_surface, monkeypatch):
    _host, widget = input_surface
    parent = widget.parentWidget()
    assert parent.windowHandle() is not None
    point = QPoint(80, 50)
    expected = widget.mapToGlobal(point)
    moves = []

    class Recorder(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            if watched is widget and event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove):
                moves.append(event.globalPosition().toPoint())
            return False

    recorder = Recorder()
    app = QApplication.instance()
    app.installEventFilter(recorder)
    entering = True

    def post():
        # Replay the Cocoa native Leave/Enter coalescing observed at a QWidget
        # parent. The first crossing is not a child move or control hover.
        if entering:
            local = QPointF(parent.mapFromGlobal(expected))
            QCoreApplication.sendEvent(parent, QEnterEvent(local, local, QPointF(expected)))
        else:
            window, position = _qt._window_position(widget, point)
            QTest.mouseMove(window, position, 0)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert cursor.posts == [expected]
        assert moves == [], "Native-parent entry does not imply delivery to its alien child"
        entering = False
        point += QPoint(10, 10)
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert widget.mapToGlobal(point) in moves
        assert cursor.posts == [expected, widget.mapToGlobal(point)]
    finally:
        app.removeEventFilter(recorder)


@pytest.mark.parametrize("input_surface", ["window"], indirect=True)
def test_cursor_move_at_current_position_is_not_fresh_input(input_surface, monkeypatch):
    _host, widget = input_surface
    point = QPoint(10, 10)
    expected = widget.mapToGlobal(point)
    cursor = _posted_cursor(monkeypatch, expected, lambda: pytest.fail("Unexpected cursor post"))
    _qt.mouse_move(widget, point, sync_cursor=True)
    assert cursor.posts == []


@pytest.mark.parametrize("input_surface", ["window"], indirect=True)
def test_cursor_move_observes_widget_after_native_host_changes(input_surface, monkeypatch):
    host, widget = input_surface
    point = QPoint(80, 50)
    expected = widget.mapToGlobal(point)
    initial_window, _ = _qt._window_position(widget, point)
    timer = QTimer(host)
    deliveries = []

    def deliver():
        if not widget.windowHandle().isExposed():
            return
        timer.stop()
        window, position = _qt._window_position(widget, point)
        deliveries.append(window)
        QTest.mouseMove(window, position, 0)

    timer.timeout.connect(deliver)

    def post():
        # Promote the visible receiver without hiding it before its first
        # native exposure. Input transport requires a visible surface; a
        # hide/show lifecycle belongs outside an in-flight delivery model.
        widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        assert widget.isVisible()
        timer.start(80)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        _qt.mouse_move(widget, point, sync_cursor=True)
        assert cursor.posts == [expected]
        assert deliveries == [widget.windowHandle()]
        assert deliveries[0] is not initial_window
    finally:
        timer.stop()


@pytest.mark.parametrize("input_surface", ["window"], indirect=True)
@pytest.mark.parametrize("failure", ["missing-delivery", "destroyed-target"])
def test_cursor_move_cleans_observer_after_delivery_failure(input_surface, monkeypatch, failure):
    host, widget = input_surface
    point = QPoint(80, 50)
    expected = widget.mapToGlobal(point)
    app = QApplication.instance()
    installed = []
    removed = []
    install = app.installEventFilter
    remove = app.removeEventFilter

    def track_install(observer):
        installed.append(observer)
        install(observer)

    def track_remove(observer):
        removed.append(observer)
        remove(observer)

    monkeypatch.setattr(app, "installEventFilter", track_install)
    monkeypatch.setattr(app, "removeEventFilter", track_remove)
    timer = QTimer(host)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: _qt.dispose_widget(widget))

    def post():
        if failure == "destroyed-target":
            timer.start(10)

    cursor = _posted_cursor(monkeypatch, widget.mapToGlobal(QPoint(10, 10)), post)
    try:
        message = "Timed out waiting for pointer delivery" if failure == "missing-delivery" else (
            "Pointer target was destroyed during input delivery"
        )
        with pytest.raises(AssertionError, match=message):
            _qt.mouse_move(widget, point, sync_cursor=True)
        assert cursor.posts == [expected]
        assert len(installed) == 1 and removed == installed
    finally:
        timer.stop()
