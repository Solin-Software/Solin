from collections import deque
import gc
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from types import SimpleNamespace
from weakref import ref

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCursor, QEnterEvent, QGuiApplication, QMouseEvent
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.ui.qml.host import configure_qml_host
from solin.ui.qml import host as qml_host
from tests._paths import REPO_ROOT
from tests._qt import (
    dispose_widget, mouse_click, mouse_move, mouse_press, mouse_release,
    show_and_activate, wait_until,
)


def _controls_widget(source, *, parent=None):
    widget = QQuickWidget(parent)
    configure_qml_host(
        widget, type_name="AppTextField", clear_color="#000000",
        mouse_tracking=True, dismiss_text_focus_on_pointer_press=True, defer_load=True,
    )
    url = QUrl.fromLocalFile(str(Path("src/solin/qml/ControlsTest.qml").resolve()))
    component = QQmlComponent(widget.engine())
    component.setParent(widget)
    component.setData(source, url)
    root = component.create()
    assert root is not None, [item.toString() for item in component.errors()]
    widget.setContent(url, component, root)
    widget.resize(360, 300)
    widget.show()
    QTest.qWait(50)
    return widget, root


@pytest.mark.parametrize("native_surface", ["window", "ancestor", "widget"])
@pytest.mark.parametrize("overlapping_window", [False, True])
def test_pointer_input_reaches_embedded_controls_through_native_surfaces(
    request, native_surface, overlapping_window
):
    initial_cursor = QCursor.pos()
    if overlapping_window:
        other = QWidget()
        request.addfinalizer(lambda: dispose_widget(other))
        other.resize(500, 500)
        other.show()
    host = QWidget()
    request.addfinalizer(lambda: dispose_widget(host))
    host.resize(420, 360)
    if overlapping_window:
        host.move(other.pos())
    outside = QPoint(410, 350)
    QCursor.setPos(host.screen().availableGeometry().bottomRight())
    QGuiApplication.sync()
    container = QWidget(host)
    container.setGeometry(10, 10, 380, 320)
    if native_surface == "ancestor":
        container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    widget, root = _controls_widget(b'''
        import QtQuick
        Item {
            property int clicks: 0
            property alias hovered: area.containsMouse
            MouseArea {
                id: area
                anchors.fill: parent
                hoverEnabled: true
                onClicked: parent.clicks++
            }
        }
    ''', parent=container)
    if native_surface == "widget":
        widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    crossings = deque(maxlen=80)
    surface_entries: list[QPoint] = []
    stage = "show host"

    def surfaces():
        return {
            "host": host, "container": container, "widget": widget,
            "quick_window": widget.quickWindow(), "host_window": host.windowHandle(),
            "container_window": container.windowHandle(), "widget_window": widget.windowHandle(),
        }

    class PointerTrace(QObject):
        def eventFilter(self, watched, event):
            if event.type() not in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseMove):
                return False
            if watched is container and event.type() == QEvent.Type.Enter:
                surface_entries.append(event.globalPosition().toPoint())
            name = next((name for name, surface in surfaces().items() if watched is surface), None)
            if name is not None:
                crossings.append({
                    "stage": stage, "receiver": name, "event": event.type().name,
                    "spontaneous": event.spontaneous(), "cursor": str(QCursor.pos()),
                    "local": str(event.position()) if hasattr(event, "position") else None,
                    "scene": str(event.scenePosition()) if hasattr(event, "scenePosition") else None,
                    "global": str(event.globalPosition()) if hasattr(event, "globalPosition") else None,
                    "timestamp": event.timestamp() if hasattr(event, "timestamp") else None,
                    "hover_before": root.property("hovered"),
                    "under_mouse_before": widget.underMouse(),
                })
            return False

    trace = PointerTrace(host)
    app = QApplication.instance()
    app.installEventFilter(trace)
    request.addfinalizer(lambda: app.removeEventFilter(trace))

    def diagnostic():
        current_surfaces = surfaces()
        geometry = {}
        for name, surface in current_surfaces.items():
            if surface is None:
                geometry[name] = None
                continue
            parent = surface.parent()
            state = {
                "parent": None if parent is None else next(
                    (name for name, candidate in current_surfaces.items() if parent is candidate),
                    str(parent),
                ),
                "geometry": str(surface.geometry()), "origin": str(surface.mapToGlobal(QPoint())),
                "visible": surface.isVisible(),
            }
            if isinstance(surface, QWidget):
                local = surface.mapFromGlobal(QCursor.pos())
                child = surface.childAt(local)
                state.update({
                    "hidden": surface.isHidden(), "visible_to_host": surface.isVisibleTo(host),
                    "under_mouse": surface.underMouse(), "mouse_tracking": surface.hasMouseTracking(),
                    "cursor_local": str(local),
                    "child_at_cursor": None if child is None else next(
                        (name for name, candidate in current_surfaces.items() if child is candidate),
                        str(child),
                    ),
                })
            else:
                state.update({"exposed": surface.isExposed(), "active": surface.isActive()})
            geometry[name] = state
        return (
            f"embedded control {stage} on {native_surface}, overlap={overlapping_window}: "
            f"initial_cursor={initial_cursor}, cursor={QCursor.pos()}, "
            f"root={(root.width(), root.height())}, hovered={root.property('hovered')}, "
            f"clicks={root.property('clicks')}, surfaces={geometry}, crossings={list(crossings)}"
        )

    show_and_activate(host)
    stage = "outside move"
    mouse_move(host, outside, sync_cursor=True)
    # Crossing into a native parent and moving within it are separate native
    # deliveries. Observe the entry before the next physical movement so the
    # embedded control is tested through the established native surface.
    stage = "surface entry"
    entry = QPoint(30, 30)
    expected_entry = widget.mapToGlobal(entry)
    surface_entries.clear()
    mouse_move(widget, entry, sync_cursor=True)
    wait_until(lambda: expected_entry in surface_entries, description=diagnostic)
    stage = "inside move"
    mouse_move(widget, QPoint(60, 60), sync_cursor=True)
    wait_until(
        lambda: root.property("hovered"),
        description=diagnostic,
    )
    stage = "click"
    mouse_click(widget, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    wait_until(lambda: root.property("clicks") == 1, description=diagnostic)
    stage = "exit move"
    mouse_move(host, outside, sync_cursor=True)
    wait_until(lambda: not root.property("hovered"), description=diagnostic)


def _native_hover_host(request, depth=1, late_native=False, owner=None, position=None, sync_cursor=False):
    host = QWidget(owner, Qt.WindowType.Tool) if owner is not None else QWidget()
    request.addfinalizer(lambda: dispose_widget(host))
    host.resize(440, 380)
    if position is not None:
        host.move(position)
    container = host
    for _ in range(depth):
        parent = QWidget(container)
        parent.setGeometry(10, 10, 400, 340)
        if not late_native:
            parent.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        container = parent
    widget, root = _controls_widget(b'''
        import QtQuick
        Item {
            property alias hovered: area.containsMouse
            property alias handlerHovered: handler.hovered
            property alias pressed: area.pressed
            property int exits: 0
            MouseArea {
                id: area
                anchors.fill: parent
                hoverEnabled: true
                onExited: parent.exits++
            }
            HoverHandler { id: handler }
        }
    ''', parent=container)
    if late_native:
        container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    show_and_activate(host)
    _enter_native_hover(widget, sync_cursor=sync_cursor)
    wait_until(lambda: root.property("hovered"), description="initial embedded hover")
    return host, container, widget, root


def _enter_native_hover(widget, inside=None, *, sync_cursor=False):
    native = widget if widget.windowHandle() is not None else widget.nativeParentWidget()
    assert native is not None and native.windowHandle() is not None
    assert QTest.qWaitForWindowExposed(native.windowHandle(), 3000), "Native input host was not exposed"
    point = inside if inside is not None else QPoint(60, 60)
    if sync_cursor:
        # Establish a fresh crossing from uncovered space into the child.
        # A second warp within the child may generate a redundant Leave/Enter
        # through overlapping native QWidget windows.
        if QGuiApplication.platformName() == "cocoa":
            # The host's uncovered corner may be owned by a different QWindow
            # on Cocoa. Cross into uncovered space in the embedded control's
            # own native parent so delivery and the ensuing Leave are observable.
            parent = widget.parentWidget()
            assert parent is not None and parent.windowHandle() is not None
            margin = QPoint(parent.width() - 2, parent.height() - 2)
            assert parent.childAt(margin) is None, "Native hover requires uncovered parent space"
            mouse_move(parent, margin, sync_cursor=True)
            wait_until(
                lambda: not widget.underMouse(),
                description="native child Leave before the next hover crossing",
            )
        else:
            host = widget.window()
            outside = QPoint(host.width() - 2, host.height() - 2)
            assert host.childAt(outside) is None, "Native hover requires uncovered host space"
            mouse_move(host, outside, sync_cursor=True)
        mouse_move(widget, point, sync_cursor=True)
        # Native preparation owns one input stream. Replaying synthetic Enter
        # events afterward would mix it with a separate Qt dispatcher state.
        return
    # These ordered-stream regressions exercise QWidgetWindow dispatch directly.
    # Physical cursor transport is covered by the native-surface tests above.
    entry = QPoint(30, 30) if point != QPoint(30, 30) else QPoint(40, 30)
    local = QPointF(widget.mapTo(native, entry))
    QCoreApplication.sendEvent(
        native.windowHandle(),
        QEnterEvent(local, local, QPointF(widget.mapToGlobal(entry))),
    )
    local = QPointF(widget.mapTo(native, point))
    QCoreApplication.sendEvent(
        native.windowHandle(),
        QMouseEvent(
            QEvent.Type.MouseMove, local, local, QPointF(widget.mapToGlobal(point)),
            Qt.MouseButton.NoButton, QGuiApplication.mouseButtons(), Qt.KeyboardModifier.NoModifier,
        ),
    )
    QCoreApplication.processEvents()


def _capture_hover_host(request):
    grabber = QWidget()
    request.addfinalizer(lambda: dispose_widget(grabber))
    grabber.resize(120, 90)
    origin = grabber.screen().availableGeometry().topLeft() + QPoint(30, 60)
    grabber.move(origin)
    show_and_activate(grabber)
    # Cocoa marks fully occluded windows unexposed. Keep the release receiver
    # beside the hover host so hiding its child cannot strand the pressed button.
    host, container, widget, root = _native_hover_host(request, position=origin + QPoint(180, 0))
    assert not grabber.frameGeometry().intersects(host.frameGeometry())
    assert QTest.qWaitForWindowExposed(grabber.windowHandle(), 3000)
    return grabber, host, container, widget, root


def _reordered_native_exit(host, container):
    outside = QPointF(host.width() - 2, host.height() - 2)
    global_position = QPointF(host.mapToGlobal(outside.toPoint()))
    # Qt can enter the outer native window before leaving its native child,
    # overwriting the last receiver with the outer window instead of its alien child.
    QCoreApplication.sendEvent(
        host.windowHandle(), QEnterEvent(outside, outside, global_position),
    )
    QCoreApplication.sendEvent(container.windowHandle(), QEvent(QEvent.Type.Leave))


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("late_native", [False, True])
def test_outer_window_entry_before_native_ancestor_exit_clears_embedded_hover(
    request, monkeypatch, depth, late_native,
):
    host, container, widget, root = _native_hover_host(request, depth, late_native)
    # Model the cursor snapshot from the native crossing without letting a real
    # asynchronous warp repair the missing QWidget exit before this assertion.
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    for cycle in range(2):
        inside = QPoint(70 + cycle * 10, 70)
        _enter_native_hover(widget, inside)
        wait_until(lambda: root.property("hovered"), description="embedded re-entry")
        assert widget.underMouse()
        _reordered_native_exit(host, container)
        wait_until(
            lambda: not root.property("hovered"),
            description=lambda cycle=cycle: (
                f"native ancestor exit cycle={cycle}, under_mouse={widget.underMouse()}, "
                f"exits={root.property('exits')}, physical={QCursor.pos()}, expected={outside}"
            ),
        )
        assert not root.property("handlerHovered")
        assert not widget.underMouse()
        assert root.property("exits") == exits + cycle + 1


@pytest.mark.parametrize("normal_exit", [False, True])
def test_ancestor_exit_preserves_inside_hover_and_does_not_duplicate_normal_exits(
    request, monkeypatch, normal_exit,
):
    _host, container, widget, root = _native_hover_host(request)
    inside = widget.mapToGlobal(QPoint(60, 60))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: inside))
    exits = root.property("exits")
    if normal_exit:
        QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    QCoreApplication.sendEvent(container, QEvent(QEvent.Type.Leave))
    QCoreApplication.processEvents()
    assert bool(root.property("hovered")) is not normal_exit
    assert root.property("exits") == exits + int(normal_exit)


def test_native_ancestor_exit_preserves_capture_until_mouse_release(request, monkeypatch):
    host, container, widget, root = _native_hover_host(request)
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    mouse_press(widget, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    try:
        assert root.property("pressed")
        _reordered_native_exit(host, container)
        QCoreApplication.processEvents()
        assert root.property("pressed")
        assert root.property("hovered")
        assert root.property("exits") == exits
    finally:
        mouse_release(widget, Qt.MouseButton.LeftButton, pos=QPoint(370, 310))
    wait_until(lambda: not root.property("hovered"), description="exit after native capture release")
    assert not root.property("pressed")
    assert not widget.underMouse()
    assert root.property("exits") == exits + 1


def test_reparenting_cancels_old_exit_and_observes_the_new_native_ancestor(request, monkeypatch):
    old_host, old_container, widget, root = _native_hover_host(request, sync_cursor=True)
    outside = old_host.mapToGlobal(QPoint(old_host.width() - 2, old_host.height() - 2))
    with monkeypatch.context() as cursor_snapshot:
        cursor_snapshot.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
        _reordered_native_exit(old_host, old_container)
        host = QWidget()
        request.addfinalizer(lambda: dispose_widget(host))
        host.resize(440, 380)
        container = QWidget(host)
        container.setGeometry(10, 10, 400, 340)
        container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        widget.setParent(container)
    widget.show()
    show_and_activate(host)
    _enter_native_hover(widget, QPoint(80, 70), sync_cursor=True)
    wait_until(lambda: root.property("hovered"), description="reparented embedded hover")
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    QCoreApplication.sendEvent(old_container, QEvent(QEvent.Type.Leave))
    QCoreApplication.processEvents()
    assert root.property("hovered")
    assert root.property("exits") == exits
    _reordered_native_exit(host, container)
    wait_until(lambda: not root.property("hovered"), description="new native ancestor exit")
    assert root.property("exits") == exits + 1


def test_pending_native_exit_resumes_after_capture_transfers_to_another_widget(request, monkeypatch):
    grabber, host, container, widget, root = _capture_hover_host(request)
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    mouse_press(widget, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    try:
        _reordered_native_exit(host, container)
        QCoreApplication.processEvents()
        grabber.grabMouse()
        assert QWidget.mouseGrabber() is grabber
        assert root.property("hovered")
        assert root.property("exits") == exits
    finally:
        try:
            mouse_release(grabber, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        finally:
            grabber.releaseMouse()
            if QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton:
                mouse_release(widget, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    wait_until(lambda: not root.property("hovered"), description="exit after foreign capture release")
    assert not widget.underMouse()
    assert root.property("exits") == exits + 1


@pytest.mark.parametrize("reenter", [False, True])
def test_normal_widget_exit_cancels_queued_native_recovery(request, monkeypatch, reenter):
    host, container, widget, root = _native_hover_host(request)
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    _reordered_native_exit(host, container)
    recovery = widget._qml_pointer_event_filter
    assert recovery._pending_exit
    assert recovery._exit_timer.isActive()
    QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert not root.property("hovered")
    assert root.property("exits") == exits + 1
    assert not recovery._pending_exit
    assert not recovery._exit_timer.isActive()
    if reenter:
        position = QPointF(60, 60)
        QCoreApplication.sendEvent(
            widget, QEnterEvent(position, position, QPointF(widget.mapToGlobal(QPoint(60, 60)))),
        )
    # Deliver a stale timeout directly so a fresh platform Enter cannot be
    # mistaken for failed exit cancellation while pumping unrelated events.
    recovery._exit_timer.timeout.emit()
    assert bool(root.property("hovered")) is reenter
    assert root.property("exits") == exits + 1
    assert not recovery._pending_exit


def test_native_exit_respects_ancestor_clipping(request, monkeypatch):
    host, container, widget, root = _native_hover_host(request)
    widget.move(250, 0)
    _enter_native_hover(widget, QPoint(60, 60))
    wait_until(lambda: root.property("hovered"), description="clipped embedded hover")
    # This point is in the child rectangle, but beyond its parent's right edge.
    clipped = widget.mapToGlobal(QPoint(200, 60))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: clipped))
    exits = root.property("exits")
    _reordered_native_exit(host, container)
    wait_until(lambda: not root.property("hovered"), description="exit outside ancestor clip")
    assert root.property("exits") == exits + 1


def test_owned_tool_window_does_not_observe_its_owners_pointer_exit(request, monkeypatch):
    owner = QWidget()
    request.addfinalizer(lambda: dispose_widget(owner))
    owner.resize(600, 440)
    show_and_activate(owner)
    host, container, widget, root = _native_hover_host(request, owner=owner, sync_cursor=True)
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    QCoreApplication.sendEvent(owner, QEvent(QEvent.Type.Leave))
    QCoreApplication.processEvents()
    assert root.property("hovered")
    assert root.property("exits") == exits
    _reordered_native_exit(host, container)
    wait_until(lambda: not root.property("hovered"), description="owned tool native ancestor exit")
    assert not widget.underMouse()


def test_native_hover_preparation_establishes_a_fresh_crossing_from_inside(request):
    _host, _container, widget, root = _native_hover_host(request, sync_cursor=True)
    exits = root.property("exits")

    _enter_native_hover(widget, sync_cursor=True)

    assert root.property("hovered")
    assert root.property("exits") == exits + 1


@pytest.mark.parametrize("exit_ancestor", ["container", "replacement_parent"])
def test_ancestor_reparenting_rebinds_pointer_exit_observation(request, monkeypatch, exit_ancestor):
    # Qualify native hover before changing the hierarchy. A synthetic Enter
    # initializes QWidget state without establishing Cocoa's native tracking.
    host, container, widget, root = _native_hover_host(request, depth=2, sync_cursor=True)
    old_parent = container.parentWidget()
    parent = QWidget(host)
    parent.setGeometry(10, 10, 400, 340)
    parent.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    container.setParent(parent)
    # The detached native sibling still overlaps the replacement input surface.
    # Keep it alive for the stale-observer assertion without intercepting input.
    old_parent.hide()
    parent.show()
    container.show()
    widget.show()
    show_and_activate(host)
    _enter_native_hover(widget, QPoint(90, 70), sync_cursor=True)
    wait_until(lambda: root.property("hovered"), description="ancestor-reparented hover")
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    exits = root.property("exits")
    QCoreApplication.sendEvent(old_parent, QEvent(QEvent.Type.Leave))
    QCoreApplication.processEvents()
    assert root.property("hovered")
    assert root.property("exits") == exits
    _reordered_native_exit(host, parent if exit_ancestor == "replacement_parent" else container)
    wait_until(lambda: not root.property("hovered"), description="rebound ancestor exit")
    assert root.property("exits") == exits + 1


def test_hiding_or_destroying_a_host_cancels_capture_recovery(request, monkeypatch):
    grabber, host, container, widget, root = _capture_hover_host(request)
    outside = host.mapToGlobal(QPoint(host.width() - 2, host.height() - 2))
    monkeypatch.setattr(qml_host, "QCursor", SimpleNamespace(pos=lambda: outside))
    mouse_press(widget, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
    try:
        _reordered_native_exit(host, container)
        QCoreApplication.processEvents()
        widget.hide()
        exits = root.property("exits")
        mouse_release(grabber, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        QCoreApplication.processEvents()
        assert root.property("exits") == exits
        dispose_widget(widget)
        # A later release on another receiver must not call the destroyed host.
        QCoreApplication.sendEvent(grabber, QEvent(QEvent.Type.UngrabMouse))
        QCoreApplication.processEvents()
    finally:
        if QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton:
            mouse_release(grabber, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))


def test_pointer_observers_do_not_retain_their_widget_owners(capfd):
    def create_owner():
        host = QWidget()
        container = QWidget(host)
        widget, _root = _controls_widget(b'import QtQuick; Item {}', parent=container)
        host.qml = widget
        container.qml = widget
        return ref(host), ref(container), ref(widget)

    for _ in range(8):
        owners = create_owner()
        gc.collect()
        assert all(owner() is None for owner in owners), "Pointer observation retained its owner tree"
        QCoreApplication.processEvents()
    assert "Error calling Python override" not in capfd.readouterr().err


@pytest.mark.parametrize("offset", [QPoint(0, 0), QPoint(80, 120)])
def test_enter_events_use_embedded_qml_coordinates(request, offset):
    host = QWidget()
    request.addfinalizer(lambda: dispose_widget(host))
    host.resize(500, 500)
    widget, root = _controls_widget(b'''
        import QtQuick
        Item {
            property alias hovered: area.containsMouse
            property int exits: 0
            MouseArea {
                id: area
                x: 30; y: 30; width: 20; height: 20
                hoverEnabled: true
                onExited: parent.exits++
            }
        }
    ''', parent=host)
    widget.move(offset)
    show_and_activate(host)
    QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert not root.property("hovered")
    position = QPointF(40, 40)
    scene_position = QPointF(widget.mapTo(host, position.toPoint()))
    global_position = QPointF(widget.mapToGlobal(position.toPoint()))
    event = QEnterEvent(position, scene_position, global_position)
    QCoreApplication.sendEvent(widget, event)
    assert root.property("hovered"), (position, scene_position, global_position)
    exits = root.property("exits")
    # Popup dismissal can deliver another Enter without a subsequent MouseMove.
    QCoreApplication.sendEvent(widget, QEnterEvent(position, scene_position, global_position))
    assert root.property("hovered")
    assert root.property("exits") == exits
    QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert not root.property("hovered")
    assert root.property("exits") == exits + 1


def test_native_graphics_are_released_between_test_protocols(tmp_path):
    child = tmp_path / "test_graphics_lifetime.py"
    child.write_text(dedent('''
        import pytest
        import shiboken6
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent
        from PySide6.QtQuickWidgets import QQuickWidget
        from PySide6.QtWidgets import QApplication
        from tests._qt import dispose_widget

        previous = []

        @pytest.mark.parametrize("cycle", range(128))
        def test_render_and_destroy(request, cycle):
            if previous:
                assert not shiboken6.isValid(previous.pop()), "Previous host outlived its test protocol"
            widget = QQuickWidget()
            def cleanup():
                dispose_widget(widget)
                assert not shiboken6.isValid(widget)
            request.addfinalizer(widget.deleteLater if cycle % 2 else cleanup)
            previous.append(widget)
            component = QQmlComponent(widget.engine())
            component.setParent(widget)
            component.setData(b'import QtQuick; Rectangle { width: 64; height: 64; color: "red" }', QUrl())
            root = component.create()
            assert root is not None, component.errors()
            widget.setContent(QUrl(), component, root)
            widget.resize(64, 64)
            widget.show()
            QApplication.processEvents()
            assert not widget.grabFramebuffer().isNull()
    '''), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(child), "-q",
            "-p", "tests.conftest", "-c", str(REPO_ROOT / "pyproject.toml"),
            f"--confcutdir={tmp_path}", f"--basetemp={tmp_path / 'graphics-tmp'}",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "128 passed" in result.stdout


def test_shared_text_field_cursor_icon_click_clear_and_outside_focus():
    widget, root = _controls_widget(b'''
        import QtQuick
        Item {
            AppTextField {
                objectName: "input"
                x: 20; y: 20; width: 280
                searchIcon: true
                clearActionVisible: text.length > 0
                onClearRequested: { text = ""; forceActiveFocus() }
            }
        }
    ''')
    try:
        show_and_activate(widget)
        field = root.findChild(QObject, "input")
        pointer = field.findChild(QObject, "textFieldPointer")
        # Establish a fresh crossing through the exposed native host, including
        # when a preceding subprocess moved the system cursor or took focus.
        mouse_move(widget, QPoint(330, 220), sync_cursor=True)
        mouse_move(widget, QPoint(38, 42), sync_cursor=True)
        wait_until(lambda: pointer.property("hovered"), description="text field hover")
        assert pointer.property("cursorShape") == Qt.CursorShape.IBeamCursor
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=QPoint(38, 42))
        assert field.property("activeFocus") is True
        QTest.keyClicks(widget, "hello")
        assert field.property("text") == "hello"
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=QPoint(278, 42))
        assert field.property("text") == ""
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=QPoint(330, 220))
        assert field.property("activeFocus") is False
    finally:
        dispose_widget(widget)


def test_shared_selection_list_reserves_scrollbar_space_and_preserves_elastic_bounds():
    widget, root = _controls_widget(b'''
        import QtQuick
        import QtQuick.Controls
        Item {
            AppSelectionList {
                id: list
                objectName: "list"
                property bool elasticBounds: boundsBehavior === Flickable.DragAndOvershootBounds
                width: 280; height: 120
                model: 12
                delegate: ItemDelegate {
                    objectName: "choice"
                    width: list.delegateWidth; height: 44
                    text: "A choice"
                }
            }
        }
    ''')
    try:
        listing = root.findChild(QObject, "list")
        assert listing.property("delegateWidth") == 266
        assert listing.property("elasticBounds") is True
        assert listing.property("contentHeight") > listing.property("height")
    finally:
        widget.close()
        widget.deleteLater()
        QApplication.processEvents()
