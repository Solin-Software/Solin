from __future__ import annotations

from types import SimpleNamespace
import sys
import traceback

import pytest
import shiboken6
from PySide6.QtCore import QObject, QPoint, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from solin.core.foundation.settings_store import ProfileAppSettingsStore, SettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.integrations.automation.settings import OBSSettingsStore
from solin.core.scenes.presets import SceneSeedNames
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.ui.popup_hover import PopupHoverController
from solin.widgets.common.popup_hover_button import PopupHoverButton
from solin.widgets.quick_access_toolbar import QuickAccessToolbar


def _activate(widget):
    widget.activateWindow()
    QTest.qWait(20)
    assert widget.isActiveWindow()


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
    owner.show()
    _activate(owner)
    popup = QWidget(owner, Qt.WindowType.Popup)
    popup.resize(180, 90)
    popup.move(owner.mapToGlobal(QPoint(0, -100)))
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
    QCursor.setPos(owner.mapToGlobal(QPoint(15, 15)))
    yield SimpleNamespace(
        owner=owner, popup=popup, controller=controller, button=button, state=state
    )
    popup.close()
    owner.close()
    owner.deleteLater()
    QApplication.processEvents()


def _enter(hover):
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
    QTest.qWait(60)
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
        QCursor.setPos(hover.owner.mapToGlobal(QPoint(60, 60)))
        hover.controller.cancel()
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
    QTest.qWait(60)
    assert hover.popup.isVisible()
    # A native popup grab can synthesize a Leave while the cursor is on the icon.
    hover.controller.cancel()
    QTest.keyClick(hover.popup, Qt.Key.Key_Escape)
    assert not hover.popup.isVisible()
    _activate(hover.owner)
    _enter(hover)
    QTest.qWait(60)
    assert hover.state.opens == 1
    QCursor.setPos(hover.owner.mapToGlobal(QPoint(60, 60)))
    hover.controller.cancel()
    QCursor.setPos(hover.owner.mapToGlobal(QPoint(15, 15)))
    _enter(hover)
    QTest.qWait(60)
    assert hover.state.opens == 2


def test_leaving_icon_to_use_popup_does_not_close_it(hover):
    hover.button.click()
    _enter(hover)
    QTest.qWait(60)
    QCursor.setPos(hover.popup.mapToGlobal(QPoint(20, 20)))
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
    QCursor.setPos(alternate.mapToGlobal(QPoint(15, 15)))
    _enter(hover)
    QTest.qWait(60)
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
    QCursor.setPos(owner.mapToGlobal(QPoint(15, 15)))
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


def _scene_runtime(tmp_path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", profile_id="hover-test"
    )
    paths.ensure_dirs()
    workspace = SceneWorkspaceService(
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
        state={"type": "idle"}, session_id=0, subscribe=lambda _: lambda: None
    )
    return SceneRuntimeController(workspace, projection)


@pytest.mark.parametrize("popup_id", ["obs", "scenes"])
def test_qml_toolbar_hover_click_keyboard_and_tooltip_share_native_popup(
    settings, tmp_path, popup_id
):
    parent = QWidget()
    parent.resize(720, 480)
    toolbar = QuickAccessToolbar(
        _Obs(parent),
        None,
        parent,
        obs_settings=OBSSettingsStore(settings.settings),
        app_settings=settings,
        scene_runtime=_scene_runtime(tmp_path),
    )
    toolbar.set_obs_connected(True)
    toolbar.qml_load_handle.start()
    parent.show()
    toolbar.show()
    _activate(parent)
    QTest.qWait(100)
    assert toolbar.status() == QQuickWidget.Status.Ready, toolbar.errors()
    toolbar.reposition()
    surface_size = toolbar.size()
    root = toolbar.rootObject()
    button_name = "toolbarObsButton" if popup_id == "obs" else "toolbarScenesButton"
    popup_attr = "_scene_popup" if popup_id == "obs" else "_solin_scene_popup"
    button = root.findChild(QQuickItem, button_name)
    assert button is not None
    point = button.mapToItem(root, QPointF(15, 15)).toPoint()
    QCursor.setPos(toolbar.mapToGlobal(point))
    QTest.mouseMove(toolbar, point)
    QTest.qWait(350)
    assert getattr(toolbar, popup_attr) is None
    QTest.mouseClick(toolbar, Qt.MouseButton.LeftButton, pos=point)
    popup = getattr(toolbar, popup_attr)
    assert popup is not None and popup.isVisible()
    assert popup.hover_button.isVisible()
    popup.hover_button.click()
    popup.close()
    _activate(parent)
    toolbar._reset_qml_pointer_cursor()
    QCursor.setPos(parent.mapToGlobal(QPoint(20, 20)))
    QTest.mouseMove(parent, QPoint(20, 20))
    QTest.qWait(40)  # Let the native window system deliver the pointer's exit.
    QCursor.setPos(toolbar.mapToGlobal(point))
    tooltip_spy = QSignalSpy(toolbar._bridge.tooltipRequested)
    QTest.mouseMove(toolbar, point)
    QTest.qWait(850)
    assert popup.isVisible()
    assert tooltip_spy.count() == 0
    popup.close()
    _activate(parent)
    toolbar.setFocus()
    button.forceActiveFocus()
    QTest.keyClick(toolbar, Qt.Key.Key_Return)
    assert popup.isVisible()
    assert toolbar.size() == surface_size
    popup.close()
    parent.close()
    parent.deleteLater()
    QApplication.processEvents()
