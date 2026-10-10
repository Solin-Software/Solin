from __future__ import annotations

from types import SimpleNamespace
import sys
import traceback

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, QObject, QPoint, QPointF, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from solin.core.foundation.settings_store import ProfileAppSettingsStore, SettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.integrations.automation.settings import OBSSettingsStore
from solin.core.scenes.presets import SceneSeedNames
from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.ui.popup_hover import PopupHoverController
from solin.widgets.common.popup_hover_button import PopupHoverButton
from solin.widgets.quick_access_toolbar import QuickAccessToolbar
from tests._qt import mouse_click, mouse_move, wait_for_geometry, wait_until


def _wait_until(predicate, timeout=2000):
    timer = QElapsedTimer()
    timer.start()
    while True:
        QApplication.processEvents()
        if predicate():
            return
        assert timer.elapsed() < timeout, "Timed out waiting for Qt state"
        QTest.qWait(5)


def _activate(widget):
    QApplication.processEvents()
    widget.raise_()
    widget.activateWindow()
    assert QTest.qWaitForWindowActive(widget, 2000)


def _move_cursor(widget, position):
    QApplication.processEvents()
    target = widget.mapToGlobal(position)
    assert widget.screen().geometry().contains(target), target
    QCursor.setPos(target)
    _wait_until(lambda: QCursor.pos() == target)
    QTest.mouseMove(widget, position)
    _wait_until(lambda: QCursor.pos() == target)


def _wait_for_popup(hover, opens=1):
    _wait_until(lambda: hover.popup.isVisible() and hover.state.opens == opens)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        SettingsStore,
        "_settings",
        lambda store: QSettings(
            str(tmp_path / f"{store.namespace.organization}-{store.namespace.application}.ini"),
            QSettings.Format.IniFormat,
        ),
    )
    return ProfileAppSettingsStore.for_organization("hover-test")


def test_hover_preferences_survive_reload_and_keep_popups_and_profiles_independent(settings):
    assert not settings.hover_popup_ids()
    settings.set_popup_hover_enabled("obs", True)
    reloaded = ProfileAppSettingsStore.for_organization("hover-test")
    assert reloaded.hover_popup_ids() == {"obs"}
    reloaded.set_popup_hover_enabled("scenes", True)
    reloaded.set_popup_hover_enabled("obs", False)
    assert settings.hover_popup_ids() == {"scenes"}
    assert not ProfileAppSettingsStore.for_organization("another-profile").hover_popup_ids()


@pytest.mark.parametrize("invalid", [None, True, 42, "obs", {"obs": True}])
def test_invalid_saved_hover_preferences_fail_closed(settings, invalid):
    settings.settings.set_value(SettingsKey.TOOLBAR_HOVER_POPUPS, invalid)
    assert not settings.hover_popup_ids()


def test_saved_hover_preferences_ignore_invalid_entries(settings):
    settings.settings.set_value(SettingsKey.TOOLBAR_HOVER_POPUPS, [None, 42, "", "obs", "obs"])
    assert settings.hover_popup_ids() == {"obs"}


@pytest.fixture
def hover(settings):
    owner = QWidget()
    owner.resize(400, 240)
    owner.move(owner.screen().availableGeometry().center() - owner.rect().center())
    owner.show()
    _activate(owner)
    popup = QWidget(owner, Qt.WindowType.Popup)
    popup.resize(180, 90)
    # Keep both the anchor and popup inside the screen, including native Cocoa.
    popup.move(owner.mapToGlobal(QPoint(0, 60)))
    controller = PopupHoverController(owner, settings, lambda: owner)
    controller._timer.setInterval(30)
    state = SimpleNamespace(available=True, opens=0)

    def open_popup():
        state.opens += 1
        popup.show()

    controller.register("example", open_popup, lambda: state.available)
    button = PopupHoverButton(owner)
    button.move(200, 120)
    controller.bind_button("example", button)
    try:
        _move_cursor(owner, QPoint(15, 15))
        yield SimpleNamespace(
            owner=owner, popup=popup, controller=controller, button=button, state=state
        )
    finally:
        popup.close()
        owner.close()
        owner.deleteLater()
        QApplication.processEvents()


def _enter(hover):
    _wait_until(hover.owner.isActiveWindow)
    hover.controller.enter("example", 0, 0, 30, 30)


def test_hover_requires_opt_in_and_ignores_unregistered_targets(hover):
    _enter(hover)
    QTest.qWait(60)
    assert not hover.popup.isVisible()
    hover.button.click()
    hover.controller.enter("unregistered", 0, 0, 30, 30)
    QTest.qWait(60)
    assert not hover.popup.isVisible()
    _enter(hover)
    _wait_for_popup(hover)
    assert hover.popup.isVisible()
    assert hover.state.opens == 1


def test_hover_toggle_is_accessible_by_keyboard_and_persists(settings, hover):
    assert not hover.button.isChecked()
    hover.button.setFocus()
    QTest.keyClick(hover.button, Qt.Key.Key_Space)
    assert hover.button.isChecked()
    assert hover.button.accessibleName() == "Open on hover"
    assert "disable" in hover.button.toolTip()
    assert settings.hover_popup_ids() == {"example"}
    hover.button.click()
    assert "enable" in hover.button.toolTip()
    assert not settings.hover_popup_ids()


@pytest.mark.parametrize("interruption", ["exit", "disable", "unavailable", "hide", "key", "click"])
def test_pending_hover_is_cancelled_or_revalidated(hover, interruption):
    hover.button.click()
    _enter(hover)
    if interruption == "exit":
        hover.controller.cancel()
        _move_cursor(hover.owner, QPoint(60, 60))
    elif interruption == "disable":
        hover.button.click()
    elif interruption == "unavailable":
        hover.state.available = False
    elif interruption == "hide":
        hover.owner.hide()
    elif interruption == "key":
        QTest.keyClick(hover.owner, Qt.Key.Key_Tab)
    elif interruption == "click":
        QTest.mouseClick(hover.owner, Qt.MouseButton.LeftButton, pos=QPoint(15, 15))
    QTest.qWait(60)
    assert hover.state.opens == 0


@pytest.mark.parametrize("receiver", ["widget", "window"])
def test_unrelated_popup_deactivation_preserves_pending_hover(hover, receiver):
    other = QWidget(hover.owner, Qt.WindowType.Popup)
    try:
        other.winId()
        hover.button.click()
        _enter(hover)
        assert hover.controller._timer.isActive()
        watched = other if receiver == "widget" else other.windowHandle()
        QApplication.sendEvent(watched, QEvent(QEvent.Type.WindowDeactivate))
        assert hover.owner.isActiveWindow()
        assert hover.controller._timer.isActive()
        _wait_for_popup(hover)
    finally:
        other.close()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(other, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("receiver", ["widget", "window"])
def test_owner_deactivation_cancels_pending_hover(hover, receiver):
    hover.button.click()
    _enter(hover)
    assert hover.controller._timer.isActive()
    watched = hover.owner if receiver == "widget" else hover.owner.windowHandle()
    QApplication.sendEvent(watched, QEvent(QEvent.Type.WindowDeactivate))
    assert not hover.controller._timer.isActive()
    QTest.qWait(60)
    assert hover.state.opens == 0


@pytest.mark.parametrize("overlay", ["popup", "modal", "inactive"])
def test_hover_does_not_interrupt_other_windows(hover, overlay):
    hover.button.click()
    _enter(hover)
    if overlay == "popup":
        other = QWidget(hover.owner, Qt.WindowType.Popup)
    else:
        other = QDialog(hover.owner) if overlay == "modal" else QWidget()
        if overlay == "modal":
            other.setModal(True)
    other.show()
    _activate(other)
    QTest.qWait(60)
    assert hover.state.opens == 0
    other.close()
    other.deleteLater()


def test_escape_does_not_reopen_until_pointer_leaves_icon(hover):
    hover.button.click()
    _enter(hover)
    _wait_for_popup(hover)
    assert hover.popup.isVisible()
    # A native popup grab can synthesize a Leave while the cursor is on the icon.
    hover.controller.cancel()
    QTest.keyClick(hover.popup, Qt.Key.Key_Escape)
    _wait_until(lambda: not hover.popup.isVisible())
    _activate(hover.owner)
    _enter(hover)
    QTest.qWait(60)
    assert hover.state.opens == 1
    _move_cursor(hover.owner, QPoint(60, 60))
    hover.controller.cancel()
    _move_cursor(hover.owner, QPoint(15, 15))
    _enter(hover)
    _wait_for_popup(hover, opens=2)
    assert hover.state.opens == 2


def test_leaving_icon_to_use_popup_does_not_close_it(hover):
    hover.button.click()
    _enter(hover)
    _wait_for_popup(hover)
    _move_cursor(hover.popup, QPoint(20, 20))
    hover.controller.cancel()
    QTest.qWait(60)
    assert hover.popup.isVisible()
    assert hover.state.opens == 1


def test_switching_render_surface_cancels_stale_hover(hover):
    hover.button.click()
    _enter(hover)
    alternate = QWidget(hover.owner)
    alternate.resize(100, 40)
    alternate.move(50, 50)
    alternate.show()
    hover.controller._surface = lambda: alternate
    QTest.qWait(60)
    assert hover.state.opens == 0
    _move_cursor(alternate, QPoint(15, 15))
    _enter(hover)
    _wait_for_popup(hover)
    assert hover.state.opens == 1


@pytest.mark.parametrize("already_opened", [False, True])
def test_owner_destruction_removes_filter_before_other_native_surfaces_close(
    settings, monkeypatch, already_opened
):
    errors = []
    monkeypatch.setattr(
        sys, "excepthook", lambda *error: errors.append("".join(traceback.format_exception(*error)))
    )
    owner = QWidget()
    owner.resize(150, 40)
    owner.show()
    other = QWidget()
    other.show()
    _activate(owner)
    controller = PopupHoverController(owner, settings, lambda: owner)
    controller.register("example", lambda: None, lambda: True)
    controller.set_enabled("example", True)
    _move_cursor(owner, QPoint(15, 15))
    controller.enter("example", 0, 0, 30, 30)
    if already_opened:
        controller._open_pending()
    owner.destroyed.connect(lambda: other.hide())
    shiboken6.delete(owner)
    other.close()
    other.deleteLater()
    assert not errors, "\n".join(errors)


class _Obs(QObject):
    recording_state_changed = Signal(bool)
    is_connected = True
    scenes = ["Camera", "Media"]
    current_scene = "Camera"


def _scene_runtime(tmp_path, scene_workspace_factory):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", profile_id="hover-test"
    )
    paths.ensure_dirs()
    workspace = scene_workspace_factory(
        paths,
        seed_names=SceneSeedNames(
            content_source="Content",
            default_camera_source="Camera",
            no_signal_source="No signal",
            content_scene="Content",
            camera_scene="Camera",
            content_camera_pip_scene="Content + camera",
            no_signal_scene="No signal",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
    )
    projection = SimpleNamespace(
        state={"type": "idle"}, session_id=0, subscribe=lambda _: lambda: None,
        idle_media_path="",
    )
    projection.set_idle_media_path = lambda path: setattr(projection, "idle_media_path", path)
    return SceneRuntimeController(workspace, projection)


@pytest.fixture
def toolbar_host(request, settings, tmp_path, scene_workspace_factory):
    scene_runtime = _scene_runtime(tmp_path, scene_workspace_factory)
    request.addfinalizer(scene_runtime.close)
    parent = QWidget()

    def destroy_parent():
        try:
            if shiboken6.isValid(parent):
                parent.close()
        finally:
            if shiboken6.isValid(parent):
                parent.deleteLater()
                QCoreApplication.sendPostedEvents(parent, QEvent.Type.DeferredDelete)

    request.addfinalizer(destroy_parent)
    parent.resize(720, 480)
    # Start outside before exposing the surface. An initial Enter generated by
    # showing a window does not advance Qt's last MouseMove position; warping
    # back to that position afterwards can be discarded as a duplicate move.
    QCursor.setPos(parent.mapToGlobal(QPoint(20, 20)))
    QGuiApplication.sync()
    toolbar = QuickAccessToolbar(
        _Obs(parent),
        None,
        parent,
        obs_settings=OBSSettingsStore(settings.settings),
        app_settings=settings,
        scene_runtime=scene_runtime,
    )
    def unload_toolbar():
        if shiboken6.isValid(toolbar):
            toolbar.qml_load_handle.cancel()
            toolbar.setSource(QUrl())

    request.addfinalizer(unload_toolbar)
    return parent, toolbar


@pytest.fixture
def toolbar_pointer_trace(toolbar_host):
    _parent, toolbar = toolbar_host
    initial_cursor = QCursor.pos()
    crossings = []
    surfaces = (toolbar, toolbar.quickWindow())

    class CrossingTrace(QObject):
        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
                position = (
                    (event.position(), event.scenePosition(), event.globalPosition())
                    if event.type() == QEvent.Type.Enter
                    else None
                )
                crossings.append(
                    (
                        "widget" if watched is toolbar else "quickWindow",
                        event.type().name,
                        QCursor.pos(),
                        position,
                    )
                )
            return False

    trace = CrossingTrace()
    for surface in surfaces:
        surface.installEventFilter(trace)
    try:
        yield initial_cursor, crossings
    finally:
        for surface in surfaces:
            if shiboken6.isValid(surface):
                surface.removeEventFilter(trace)


@pytest.mark.parametrize("popup_id", ["obs", "scenes"])
def test_qml_toolbar_hover_click_keyboard_and_tooltip_share_native_popup(
    toolbar_host, toolbar_pointer_trace, popup_id
):
    parent, toolbar = toolbar_host
    initial_cursor, crossings = toolbar_pointer_trace
    toolbar.set_obs_connected(True)
    toolbar.qml_load_handle.start()
    parent.show()
    toolbar.show()
    _activate(parent)
    wait_until(
        lambda: toolbar.status() == QQuickWidget.Status.Ready,
        description="toolbar QML load",
    )
    toolbar.reposition()
    surface_size = toolbar.size()
    wait_for_geometry(toolbar, size=surface_size)
    root = toolbar.rootObject()
    button_name = "toolbarObsButton" if popup_id == "obs" else "toolbarScenesButton"
    popup_attr = "_scene_popup" if popup_id == "obs" else "_solin_scene_popup"
    button = root.findChild(QQuickItem, button_name)
    assert button is not None
    # Hover entry requires a transition from outside the loaded QML surface.
    mouse_move(parent, QPoint(20, 20), sync_cursor=True)
    wait_until(
        lambda: (
            not toolbar.rect().contains(toolbar.mapFromGlobal(QCursor.pos()))
            and root.property("tooltipItem") is None
        ),
        description=lambda: (
            f"initial toolbar hover exit: initial_cursor={initial_cursor}, "
            f"cursor={QCursor.pos()}, local={toolbar.mapFromGlobal(QCursor.pos())}, "
            f"rect={toolbar.rect()}, under_mouse={toolbar.underMouse()}, "
            f"tooltip_item={root.property('tooltipItem')}, crossings={crossings}"
        ),
    )
    point = button.mapToItem(root, QPointF(15, 15)).toPoint()
    mouse_move(toolbar, point, sync_cursor=True)
    wait_until(
        lambda: root.property("tooltipItem") is not None,
        description=lambda: (
            f"toolbar hover entry: point={point}, global={toolbar.mapToGlobal(point)}, "
            f"cursor={QCursor.pos()}, widget={toolbar.geometry()}, "
            f"root={(root.width(), root.height())}, "
            f"button={(button.x(), button.y(), button.width(), button.height())}, "
            f"under_mouse={toolbar.underMouse()}, active={parent.isActiveWindow()}, "
            f"initial_cursor={initial_cursor}, crossings={crossings}"
        ),
    )
    QTest.qWait(350)
    assert getattr(toolbar, popup_attr) is None
    mouse_click(toolbar, Qt.MouseButton.LeftButton, pos=point)
    popup = getattr(toolbar, popup_attr)
    assert popup is not None and popup.isVisible()
    assert popup.hover_button.isVisible()
    popup.hover_button.click()
    popup.close()
    _activate(parent)
    toolbar._reset_qml_pointer_cursor()
    mouse_move(parent, QPoint(20, 20), sync_cursor=True)
    wait_until(lambda: root.property("tooltipItem") is None, description="toolbar hover exit")
    tooltip_spy = QSignalSpy(toolbar._bridge.tooltipRequested)
    hover_spy = QSignalSpy(toolbar._bridge.popupHoverRequested)
    cancel_spy = QSignalSpy(toolbar._bridge.popupHoverCancelled)
    opening_spy = QSignalSpy(toolbar._popup_hover.opening)
    mouse_move(toolbar, point, sync_cursor=True)
    wait_until(
        popup.isVisible,
        description=lambda: (
            f"popup opening after hover: requests={hover_spy.count()}, "
            f"cancellations={cancel_spy.count()}, openings={opening_spy.count()}, "
            f"pending={toolbar._popup_hover._pending}, "
            f"timer={toolbar._popup_hover._timer.remainingTime()}, "
            f"blocked={toolbar._popup_hover._blocked}, "
            f"enabled={toolbar._popup_hover._enabled_ids}, "
            f"tooltip_item={root.property('tooltipItem')}, "
            f"cached_point={point}, current_point={button.mapToItem(root, QPointF(15, 15))}, "
            f"cursor={QCursor.pos()}, local={toolbar.mapFromGlobal(QCursor.pos())}, "
            f"surface={toolbar.geometry()}, root={(root.width(), root.height())}, "
            f"active={parent.isActiveWindow()}, active_popup={QApplication.activePopupWidget()}, "
            f"active_modal={QApplication.activeModalWidget()}, buttons={QApplication.mouseButtons()}, "
            f"initial_cursor={initial_cursor}, crossings={crossings}"
        ),
    )
    # Observe the tooltip delay to ensure opening the popup cancels it.
    QTest.qWait(750)
    assert popup.isVisible()
    assert tooltip_spy.count() == 0
    popup.close()
    _activate(parent)
    toolbar.setFocus()
    button.forceActiveFocus()
    QTest.keyClick(toolbar, Qt.Key.Key_Return)
    wait_until(popup.isVisible, description="popup opening by keyboard")
    assert toolbar.size() == surface_size
    popup.close()
