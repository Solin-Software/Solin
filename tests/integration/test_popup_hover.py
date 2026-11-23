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
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

from solin.core.foundation.settings_store import ProfileAppSettingsStore, SettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.integrations.automation.auto_key_actions import (
    AutoKeyAction,
    EVENT_MEDIA_STARTED,
    EVENT_ZOOM_AFTER_MEETING,
    EVENT_ZOOM_BEFORE_MEETING,
    EVENT_ZOOM_MEETING_STARTED,
)
from solin.core.integrations.automation.settings import (
    AutoKeySettingsStore, OBSSettingsStore, ZoomSettingsStore,
)
from solin.core.integrations.automation.zoom.service import ZoomService
from solin.core.integrations.automation.zoom.state import (
    AudioState,
    ParticipantAction,
    RaisedHand,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
)
from solin.core.scenes.presets import SceneSeedNames
from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.ui.popup_hover import PopupHoverController
from solin.ui.incremental_load import IncrementalLoadState
from solin.widgets import quick_access_toolbar
from solin.widgets.background_song_popup import BackgroundSongPopup
from solin.widgets.common import toolbar_panel
from solin.widgets.common.popup_hover_button import PopupHoverButton
from solin.widgets.common.toolbar_panel import ToolbarPanel
from solin.widgets.quick_access_toolbar import QuickAccessToolbar
from solin.widgets.obs_scene_popup import OBSScenePopup, _SceneChipButton
from solin.widgets.scenes.control_popup import SceneControlPopup
from tests._qt import dispose_widget, mouse_click, mouse_move, wait_for_geometry, wait_until


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
def toolbar_host(request, settings, tmp_path, scene_workspace_factory, monkeypatch):
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
    zoom_settings = ZoomSettingsStore(settings.settings)
    zoom = None
    if getattr(request, "param", False):
        zoom = ZoomService(zoom_settings, parent, raised_hands_enabled=True)
        monkeypatch.setattr(ZoomService, "is_available", property(lambda _: True))
        monkeypatch.setattr(zoom, "refresh", lambda: None)
        request.addfinalizer(zoom.stop)
        zoom.start()
        zoom._set_connected(True)
    parent.resize(720, 480)
    # Start outside before exposing the surface. An initial Enter generated by
    # showing a window does not advance Qt's last MouseMove position; warping
    # back to that position afterwards can be discarded as a duplicate move.
    QCursor.setPos(parent.mapToGlobal(QPoint(20, 20)))
    QGuiApplication.sync()
    toolbar = QuickAccessToolbar(
        _Obs(parent),
        zoom,
        parent,
        obs_settings=OBSSettingsStore(settings.settings),
        auto_key_settings=AutoKeySettingsStore(settings.settings),
        app_settings=settings,
        scene_runtime=scene_runtime,
        timer_hand_alert=TimerHandAlertController(zoom_settings, parent),
    )
    def unload_toolbar():
        if shiboken6.isValid(toolbar):
            toolbar.qml_load_handle.cancel()
            toolbar.setSource(QUrl())

    request.addfinalizer(unload_toolbar)
    return parent, toolbar


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_popup_restores_and_persists_playback_microphone_preference(toolbar_host, settings):
    _parent, toolbar = toolbar_host
    service = toolbar._zoom
    service.set_mute_during_playback(True)
    toolbar._on_zoom_clicked()
    panel = toolbar._zoom_panel
    assert panel._playback_mute.isVisible() and panel._playback_mute.isChecked()
    panel._playback_mute.click()
    assert not service.mute_during_playback
    assert not ZoomSettingsStore(settings.settings).mute_during_playback()
    panel.hide()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_toolbar_popup_controls_raised_hands(toolbar_host, monkeypatch):
    _parent, toolbar = toolbar_host
    service = toolbar._zoom
    commands = []
    monkeypatch.setattr(service, "_send", lambda command, **kwargs: commands.append((command, kwargs)))
    toolbar._on_zoom_clicked()
    popup = toolbar._zoom_panel
    assert popup._meeting_page.isVisible()
    assert not popup._hands_page.isVisible()
    QTest.mouseClick(popup._tabs, Qt.MouseButton.LeftButton, pos=popup._tabs.tabRect(1).center())
    assert popup._monitor_hands.isVisible()
    assert not popup._monitor_hands.isChecked()
    assert not popup._timer_integration.isVisible()
    assert not popup._manual_hand.isVisible()
    alerts = toolbar._timer_hand_alert
    alerts.set_outputs(available=True)
    assert popup._timer_integration.isVisible()
    assert not popup._timer_integration.isChecked()
    assert popup._manual_hand.isVisible()
    assert popup._manual_hand.isEnabled()
    popup._manual_hand.click()
    assert alerts.alert.manual
    assert not alerts.integrated
    popup._timer_integration.click()
    assert alerts.integrated
    assert alerts.alert.manual
    alerts.set_manual(False)
    assert not popup._manual_hand.isChecked()
    assert not alerts.alert.manual
    assert not popup._monitor_hands.isChecked()

    popup._monitor_hands.click()
    assert popup._monitor_hands.isChecked()
    reference = "1" * 32
    hand = RaisedHand("Participant", AudioState.MUTED, reference)
    service._publish_hands(RaisedHandsSnapshot(RaisedHandsStatus.READY, (hand,)))
    popup._rows[reference].button(ParticipantAction.REQUEST_UNMUTE).click()
    assert commands == [("request_unmute", {"reference": reference})]
    service.busy_changed.emit(False)
    assert not popup._count.isHidden()
    assert popup._tabs.accessibleTabName(1) != popup._tabs.tabText(1)
    popup._tabs.setCurrentIndex(0)
    assert not popup._hands_page.isVisible()
    assert popup._count.isVisible()
    popup._tabs.setCurrentIndex(1)

    popup.hide()
    toolbar._on_zoom_clicked()
    assert popup._monitor_hands.isVisible() and popup._monitor_hands.isChecked()
    service._publish_hands(RaisedHandsSnapshot(RaisedHandsStatus.PANEL_CLOSED))
    assert not popup._hands_status.isHidden()
    assert popup._roster.isHidden()
    assert popup._count.isHidden()
    popup._monitor_hands.click()
    assert service.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE
    assert not popup._hands_status.isHidden()
    assert not popup._monitor_hands.isChecked()
    alerts.set_outputs(available=False)
    assert not popup._timer_integration.isVisible()
    assert not popup._manual_hand.isVisible()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
@pytest.mark.parametrize("is_dev", [False, True])
def test_zoom_popup_sample_names_are_dev_only(toolbar_host, monkeypatch, is_dev):
    monkeypatch.setattr("solin.controllers.timer_hand_alert_controller.IS_DEV", is_dev)
    _parent, toolbar = toolbar_host
    alerts = toolbar._timer_hand_alert
    toolbar._on_zoom_clicked()
    popup = toolbar._zoom_panel
    button = popup._demo_toggle
    if not is_dev:
        assert button is None
        return
    assert not button.isVisible()
    popup._tabs.setCurrentIndex(1)
    assert button.isVisible()
    button.click()
    assert len(popup._rows) == 15
    assert not alerts.alert.names
    assert all(
        not row.button(ParticipantAction.LOWER_HAND).isEnabled()
        for row in popup._rows.values()
    )
    assert not popup._monitor_hands.isChecked()

    alerts.set_outputs(available=True)
    assert len(alerts.alert.names) == 15
    assert not popup._timer_integration.isChecked()
    popup._manual_hand.click()
    button.click()
    assert not popup._rows
    assert alerts.alert.manual

    button.click()
    toolbar.set_zoom_enabled(False)
    assert not alerts.alert.names
    assert alerts.alert.manual
    assert not button.isChecked()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_popup_opens_from_its_button_in_the_qml_toolbar(toolbar_host):
    parent, toolbar = toolbar_host
    toolbar.qml_load_handle.start()
    parent.show()
    toolbar.show()
    _activate(parent)
    wait_until(lambda: toolbar.status() == QQuickWidget.Status.Ready, description="toolbar QML load")
    toolbar.reposition()
    root = toolbar.rootObject()
    button = root.findChild(QQuickItem, "toolbarZoomButton")
    assert button.isVisible()
    point = button.mapToItem(root, QPointF(button.width() / 2, button.height() / 2)).toPoint()
    mouse_click(toolbar, Qt.MouseButton.LeftButton, pos=point)
    popup = toolbar._zoom_panel
    assert popup is not None and popup.isVisible()
    QTest.keyClick(popup, Qt.Key.Key_Escape)
    assert not popup.isVisible()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
@pytest.mark.parametrize("embedded", [False, True], ids=["window", "embedded"])
def test_lowering_a_zoom_hand_keeps_the_panel_open_until_a_solin_click(
    toolbar_host, monkeypatch, embedded
):
    parent, toolbar = toolbar_host
    service = toolbar._zoom
    outside_button = QPushButton("Outside the panel", parent)
    outside_button.move(20, 20)
    clicked = QSignalSpy(outside_button.clicked)
    other_window = QWidget()
    other_window.show()
    parent.show()
    _activate(parent)
    commands = []

    def send(command, **kwargs):
        commands.append((command, kwargs))
        other_window.raise_()
        other_window.activateWindow()
        app = QApplication.instance()
        QApplication.sendEvent(app, QEvent(QEvent.Type.ApplicationDeactivate))

    monkeypatch.setattr(service, "_send", send)
    try:
        with monkeypatch.context() as platform:
            platform.setattr(toolbar_panel, "_uses_embedded_panels", lambda: embedded)
            toolbar._on_zoom_clicked()
        panel = toolbar._zoom_panel
        assert panel is not None
        assert panel.isWindow() is not embedded
        if embedded:
            assert parent.rect().contains(panel.geometry())
        panel._tabs.setCurrentIndex(1)
        panel._monitor_hands.click()
        reference = "1" * 32
        service._publish_hands(
            RaisedHandsSnapshot(
                RaisedHandsStatus.READY, (RaisedHand("Participant", AudioState.MUTED, reference),)
            )
        )
        lower_hand = panel._rows[reference].button(ParticipantAction.LOWER_HAND)
        QTest.mouseClick(lower_hand, Qt.MouseButton.LeftButton)
        assert commands == [("lower_hand", {"reference": reference})]
        assert QTest.qWaitForWindowActive(other_window, 2000)
        assert panel.isVisible()

        QTest.mouseClick(other_window, Qt.MouseButton.LeftButton)
        _activate(parent)
        assert panel.isVisible()

        mouse_click(outside_button, Qt.MouseButton.LeftButton, pos=outside_button.rect().center())
        assert not panel.isVisible()
        assert clicked.count() == 1
    finally:
        other_window.close()
        other_window.deleteLater()


@pytest.fixture
def embedded_panel_host(monkeypatch):
    host = QWidget()
    host.resize(900, 600)
    try:
        monkeypatch.setattr(toolbar_panel, "_uses_embedded_panels", lambda: True)
        yield host
    finally:
        dispose_widget(host)


def test_embedded_background_song_panel_stays_hidden_on_startup(embedded_panel_host):
    host = embedded_panel_host
    service = SimpleNamespace(is_playing=False)
    panel = BackgroundSongPopup(service, host, defer_build=True)
    host.show()
    QApplication.processEvents()

    assert not panel.isWindow()
    assert not panel.isVisible()


def test_embedded_panel_stays_open_on_a_press_inside(embedded_panel_host):
    host = embedded_panel_host
    panel = ToolbarPanel(host)
    panel.setGeometry(20, 20, 200, 120)
    background = QWidget(panel)
    background.setGeometry(10, 10, 100, 80)
    outside = QPushButton("Outside the panel", host)
    outside.move(300, 20)
    clicked = QSignalSpy(outside.clicked)
    host.show()
    panel._show_anchored(host)

    QTest.mouseClick(background, Qt.MouseButton.LeftButton)
    assert panel.isVisible()

    QTest.mouseClick(outside, Qt.MouseButton.LeftButton)
    assert not panel.isVisible()
    assert clicked.count() == 1


def test_embedded_panel_that_fits_stays_within_the_host(embedded_panel_host):
    host = embedded_panel_host
    anchor = QWidget(host)
    anchor.setGeometry(350, 496, 280, 40)
    panel = OBSScenePopup(host)
    panel.setFixedHeight(540)
    host.show()
    panel.show_above(anchor)

    assert panel.isVisible()
    assert host.rect().contains(panel.geometry())


def test_toolbar_destruction_cancels_queued_reposition(toolbar_host, monkeypatch):
    _parent, toolbar = toolbar_host
    QApplication.processEvents()
    callbacks = []
    monkeypatch.setattr(toolbar, "_reposition", lambda: callbacks.append(True))
    toolbar._schedule_reposition()
    toolbar.deleteLater()
    QCoreApplication.sendPostedEvents(toolbar, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(toolbar)
    QApplication.processEvents()
    assert callbacks == []


@pytest.mark.parametrize("destroy_before_delivery", [False, True], ids=["alive", "destroyed"])
def test_deferred_mac_toolbar_rounding_runs_only_while_toolbar_exists(
    toolbar_host, monkeypatch, destroy_before_delivery,
):
    _parent, toolbar = toolbar_host
    monkeypatch.setattr(toolbar, "_reposition", lambda: None)
    QApplication.processEvents()
    calls = []
    monkeypatch.setattr(quick_access_toolbar, "_MAC", True)
    monkeypatch.setattr(
        quick_access_toolbar, "apply_corner_radius", lambda widget, _radius: calls.append(widget),
    )

    toolbar._apply_mac_corners()
    assert calls == [toolbar]
    if destroy_before_delivery:
        toolbar.deleteLater()
        QCoreApplication.sendPostedEvents(toolbar, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(toolbar)

    QApplication.processEvents()
    assert calls == ([toolbar] if destroy_before_delivery else [toolbar, toolbar])


def test_toolbar_prepares_popups_without_qt_camera_dependencies(toolbar_host):
    _parent, toolbar = toolbar_host
    toolbar.auxiliary_preparation_handle.complete_now()

    assert toolbar.auxiliary_preparation_handle.state is IncrementalLoadState.READY
    assert toolbar._scene_popup is not None
    assert isinstance(toolbar._solin_scene_popup, SceneControlPopup)
    assert toolbar._solin_scene_popup._controller is toolbar._scene_runtime
    camera_scene = next(
        scene for scene in toolbar._scene_runtime.document.scenes if scene.name == "Camera"
    )
    assert camera_scene.id in toolbar._solin_scene_popup._scene_cards


@pytest.mark.parametrize("visible", [False, True])
def test_scene_panel_restores_and_persists_projection_controls(toolbar_host, settings, visible):
    _parent, toolbar = toolbar_host
    settings.set_scenes_panel_projection_controls_visible(visible)
    popup = toolbar._ensure_solin_scene_popup()
    assert popup.projection_controls_button.isChecked() is visible

    popup.projection_controls_button.click()

    reloaded = ProfileAppSettingsStore.for_organization("hover-test")
    assert reloaded.scenes_panel_projection_controls_visible() is not visible
    assert toolbar._ensure_solin_scene_popup() is popup


def test_zoom_remains_available_without_an_open_meeting(toolbar_host, settings):
    _parent, toolbar = toolbar_host
    toolbar._zoom = ZoomService(ZoomSettingsStore(settings.settings), toolbar)
    toolbar.set_zoom_enabled(True)
    toolbar.set_zoom_connected(False)
    assert toolbar._bridge._zoom_visible
    toolbar._on_zoom_clicked()
    assert toolbar._zoom_panel is not None
    assert not toolbar._zoom_panel._start.isEnabled()
    assert not toolbar._zoom_panel._combined.isEnabled()
    toolbar.set_zoom_enabled(False)
    assert not toolbar._bridge._zoom_visible
    assert not toolbar._zoom_panel.isVisible()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_popup_relays_meeting_actions_to_the_coordinator(toolbar_host, monkeypatch):
    _parent, toolbar = toolbar_host
    dispatched = []
    monkeypatch.setattr(toolbar._zoom, "request_pre_meeting", lambda: dispatched.append(True))
    before = QSignalSpy(toolbar.zoom_before_meeting_requested)
    start = QSignalSpy(toolbar.zoom_meeting_start_requested)
    finish = QSignalSpy(toolbar.zoom_after_meeting_requested)
    panel = toolbar._ensure_zoom_panel()
    panel.set_connected(True)

    panel._combined.click()
    panel._start.click()
    assert before.count() == start.count() == 1
    assert finish.count() == 0

    panel.set_split_meeting_actions(True)
    panel._before.click()
    panel._after.click()
    assert before.count() == 2
    assert finish.count() == 1
    assert dispatched == []


@pytest.mark.parametrize(
    "global_enabled,configured_actions,split",
    [
        (False, [AutoKeyAction("before", EVENT_ZOOM_BEFORE_MEETING, "Ctrl+1")], False),
        (True, [], False),
        (True, [AutoKeyAction("media", EVENT_MEDIA_STARTED, "Ctrl+1")], False),
        (True, [AutoKeyAction("start", EVENT_ZOOM_MEETING_STARTED, "Ctrl+1")], False),
        (True, [AutoKeyAction("before", EVENT_ZOOM_BEFORE_MEETING, "Ctrl+1", False)], False),
        (True, [AutoKeyAction("before", EVENT_ZOOM_BEFORE_MEETING, "Ctrl+1")], True),
        (True, [AutoKeyAction("after", EVENT_ZOOM_AFTER_MEETING, "Ctrl+1")], True),
    ],
)
@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_popup_adapts_to_enabled_automatic_shortcuts(
    toolbar_host, global_enabled, configured_actions, split,
):
    _parent, toolbar = toolbar_host
    toolbar.set_zoom_enabled(True)
    toolbar._auto_key_settings.set_enabled(global_enabled)
    toolbar._auto_key_settings.save_actions(configured_actions)

    toolbar._on_zoom_clicked()
    panel = toolbar._zoom_panel
    assert panel is not None
    assert not panel._start.isHidden()
    assert panel._before.isHidden() is (not split)
    assert panel._after.isHidden() is (not split)
    assert panel._combined.isHidden() is split
    panel.hide()


@pytest.mark.parametrize("toolbar_host", [True], indirect=True)
def test_zoom_popup_rechecks_actions_and_global_switch_on_each_open(toolbar_host):
    _parent, toolbar = toolbar_host
    toolbar.set_zoom_enabled(True)
    store = toolbar._auto_key_settings

    def open_split_state() -> bool:
        toolbar._on_zoom_clicked()
        panel = toolbar._zoom_panel
        assert panel is not None
        split = not panel._before.isHidden()
        panel.hide()
        return split

    store.set_enabled(True)
    assert not open_split_state()
    store.save_actions([AutoKeyAction("after", EVENT_ZOOM_AFTER_MEETING, "Ctrl+1")])
    assert open_split_state()
    store.set_enabled(False)
    assert not open_split_state()
    store.set_enabled(True)
    assert open_split_state()
    store.save_actions([AutoKeyAction("after", EVENT_ZOOM_AFTER_MEETING, "Ctrl+1", False)])
    assert not open_split_state()


@pytest.mark.parametrize("initial_split", [False, True])
@pytest.mark.parametrize("song_available", [False, True])
@pytest.mark.parametrize("above", [False, True])
def test_zoom_popup_stays_clear_of_toolbar_after_layout_changes(
    request, initial_split, song_available, above,
):
    from solin.widgets.zoom_panel import ZoomPanel

    anchor = QWidget(None, Qt.WindowType.FramelessWindowHint)
    request.addfinalizer(anchor.deleteLater)
    screen = anchor.screen().availableGeometry()
    anchor.resize(400, 44)
    anchor.move(screen.center().x() - 200, screen.bottom() - 70 if above else screen.top() + 30)
    anchor.show()
    QApplication.processEvents()
    panel = ZoomPanel(anchor)
    panel.set_connected(True)
    panel.set_background_song_link(song_available, False)

    def assert_placement():
        toolbar_top = anchor.mapToGlobal(QPoint(0, 0)).y()
        if above:
            assert panel.frameGeometry().bottom() + 11 == toolbar_top
        else:
            assert panel.frameGeometry().top() == toolbar_top + anchor.height() + 10
        assert screen.contains(panel.frameGeometry())

    for split in (initial_split, not initial_split, initial_split):
        panel.hide()
        panel.set_split_meeting_actions(split)
        panel.show_above(anchor)
        assert_placement()
        opening_size = panel.size()
        QApplication.processEvents()
        assert_placement()
        assert panel.size() == opening_size
        panel.set_split_meeting_actions(not split)
        assert_placement()
        QApplication.processEvents()
        assert_placement()
        assert (panel.height() > opening_size.height()) is (not split)
        panel.set_split_meeting_actions(split)
        QApplication.processEvents()
        assert_placement()
        assert panel.size() == opening_size
        for available in (not song_available, song_available):
            panel.set_background_song_link(available, False)
            QApplication.processEvents()
            assert_placement()
        panel.set_busy(True)
        QApplication.processEvents()
        assert_placement()
        panel.set_busy(False)
    panel.hide()


def test_toolbar_preserves_obs_selection_return_and_ndi_signals(toolbar_host):
    _parent, toolbar = toolbar_host
    selections = []
    returns = []
    streams = []
    toolbar.obs_scene_change.connect(selections.append)
    toolbar.obs_return_scene_change.connect(returns.append)
    toolbar.obs_stream_requested.connect(lambda: streams.append(True))
    toolbar.set_obs_stream_available(True)
    toolbar.set_obs_stream_active(True)

    popup = toolbar._ensure_scene_popup()
    assert popup._stream_btn.text() == popup.tr("Stop Stream")
    assert not popup._stream_btn.isHidden()
    popup._stream_btn.click()
    assert streams == [True]

    toolbar.set_obs_stream_available(False)
    assert not popup._stream_btn.isHidden()
    toolbar.set_obs_stream_active(False)
    assert popup._stream_btn.isHidden()
    toolbar.set_obs_stream_available(True)
    assert popup._stream_btn.text() == popup.tr("Show Stream")

    popup.populate(["Camera", "Media"], "Media", "Camera", "Media")
    camera = next(chip for chip in popup.findChildren(_SceneChipButton) if chip.text() == "Camera")
    camera.right_clicked.emit()
    assert returns == ["Camera"]
    assert selections == []
    camera.click()
    assert selections == ["Camera"]
    assert toolbar._ensure_scene_popup() is popup


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
    if popup_id == "scenes":
        mouse_click(toolbar, Qt.MouseButton.LeftButton, pos=point)
        assert not popup.isVisible()
        mouse_click(toolbar, Qt.MouseButton.LeftButton, pos=point)
        assert popup.isVisible()
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
