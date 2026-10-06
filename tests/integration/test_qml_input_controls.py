from collections import deque
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCursor, QEnterEvent, QGuiApplication
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.ui.qml.host import configure_qml_host
from tests._paths import REPO_ROOT
from tests._qt import dispose_widget, mouse_click, mouse_move, show_and_activate, wait_until


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
