from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMetaObject, QObject, Qt, QUrl, Signal
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.core.foundation.settings_store import SettingsStore
from solin.core.integrations.automation.auto_key_actions import AUTO_KEY_EVENTS, AutoKeyAction
from solin.core.integrations.automation.obs import OBSConnectionState
from solin.core.integrations.automation.settings import (
    AutoKeySettingsStore,
    AutoShareSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from solin.ui.qml.settings import integrations
from solin.ui.qml.settings.integrations import IntegrationSettings
from solin.ui.qml.host import configure_qml_controls_style, current_qml_theme


class _Obs(QObject):
    state_changed = Signal(object, str)
    scenes_updated = Signal(list)

    def __init__(self):
        super().__init__()
        self.state = OBSConnectionState.CONNECTED
        self.scenes = ["Idle", "Media"]
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class _Ndi(QObject):
    sources_ready = Signal(list)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.calls = 0

    def refresh_sources(self):
        self.calls += 1


@pytest.fixture
def setup_domain():
    settings = SettingsStore.for_namespace(f"settings-qml-{uuid.uuid4().hex}", "test")
    obs = OBSSettingsStore(settings)
    obs.set_connection(4455, "saved-secret")
    obs.set_scenes("Offline idle", "Offline media")
    obs.set_enabled(True)
    zoom = ZoomSettingsStore(settings)
    zoom.set_show_participants(False)
    share = AutoShareSettingsStore(settings)
    keys = AutoKeySettingsStore(settings)
    obs_service, ndi = _Obs(), _Ndi()
    domain = IntegrationSettings(
        obs_service=obs_service, ndi_service=ndi, obs_settings=obs,
        zoom_settings=zoom, auto_share_settings=share,
        auto_key_settings=keys, auto_share_accessibility_trusted=lambda: False,
        target_picker_factory=lambda **_kwargs: None,
    )
    yield SimpleNamespace(domain=domain, obs=obs, zoom=zoom, share=share, keys=keys,
                          service=obs_service, ndi=ndi)
    domain.cleanup()
    settings.clear()


def test_runtime_is_ready_without_instantiating_qml_and_preserves_scenes(setup_domain):
    ctx = setup_domain
    state = ctx.domain.state
    assert state["obsConnected"] is True
    assert state["obsPassword"] == ""
    assert state["obsDefaultScene"] == "Offline idle"
    assert "Offline idle" in [item["value"] for item in state["obsScenes"]]
    ctx.service.scenes_updated.emit(["New scene"])
    assert ctx.obs.default_scene() == "Offline idle"
    ctx.service.state_changed.emit(OBSConnectionState.ERROR, "Incorrect password.")
    assert ctx.domain.state["obsConnected"] is False
    assert ctx.domain.state["obsMediaSceneEnabled"] is False
    assert "Incorrect password" in ctx.domain.state["obsStatus"]
    ctx.domain.refresh_language()
    assert "Incorrect password" in ctx.domain.state["obsStatus"]


def test_integration_settings_do_not_offer_removed_camera_workflow(setup_domain):
    ctx = setup_domain
    assert "cameraAvailable" not in ctx.domain.state
    assert "cameraEnabled" not in ctx.domain.state
    ctx.domain.setValue("cameraEnabled", True)
    assert "cameraEnabled" not in ctx.domain.state
    assert ctx.domain.state["feedbackKind"] == "error"


def test_obs_debounce_preserves_password_and_reconnects_once(setup_domain):
    ctx = setup_domain
    ctx.domain.setValue("obsPort", "4500")
    QTest.qWait(450)
    assert ctx.obs.websocket_port() == 4455
    ctx.domain.setValue("obsPort", "4600")
    QTest.qWait(450)
    assert ctx.obs.websocket_port() == 4455
    QTest.qWait(500)
    assert ctx.obs.websocket_port() == 4600
    assert ctx.obs.password() == "saved-secret"
    assert ctx.service.calls == ["stop", "start"]
    assert ctx.domain.state["obsConnectionFeedback"]


@pytest.mark.parametrize("port", ["abc", "-1", "65536", "1.2"])
def test_invalid_obs_port_does_not_save_or_reconnect(setup_domain, port):
    ctx = setup_domain
    ctx.domain.setValue("obsPort", port)
    ctx.domain._obs_save_timer.stop()
    ctx.domain._save_obs_connection()
    assert ctx.obs.websocket_port() == 4455
    assert ctx.obs.password() == "saved-secret"
    assert ctx.service.calls == []
    assert ctx.domain.state["obsConnectionError"]
    assert not ctx.domain.state["obsConnectionFeedback"]


def test_password_draft_is_cleared_after_save(setup_domain):
    ctx = setup_domain
    ctx.domain.setValue("obsPassword", "new-secret")
    ctx.domain._obs_save_timer.stop()
    ctx.domain._save_obs_connection()
    assert ctx.obs.password() == "new-secret"
    assert ctx.domain.state["obsPassword"] == ""


def test_save_failure_keeps_password_draft_and_has_no_false_success(setup_domain, monkeypatch):
    ctx = setup_domain

    def fail_save(*_args):
        raise OSError("Storage unavailable")

    monkeypatch.setattr(OBSSettingsStore, "set_connection", fail_save)
    ctx.domain.setValue("obsPassword", "draft-secret")
    ctx.domain._obs_save_timer.stop()
    ctx.domain._save_obs_connection()
    assert ctx.domain.state["obsPassword"] == "draft-secret"
    assert ctx.domain.state["obsConnectionError"] == "Storage unavailable"
    assert ctx.domain.state["obsConnectionFeedback"] == ""
    assert ctx.service.calls == []


def test_ndi_discovery_preserves_saved_offline_source_and_handles_empty_error(setup_domain):
    ctx = setup_domain
    ctx.obs.set_ndi_source("Offline program")
    ctx.domain.invoke("findNdiSources")
    ctx.domain.invoke("findNdiSources")
    assert ctx.ndi.calls == 1
    assert ctx.domain.state["ndiBusy"] is True
    ctx.ndi.sources_ready.emit(["Other program"])
    assert ctx.domain.state["ndiBusy"] is False
    assert ctx.obs.ndi_source() == "Offline program"
    assert "Offline program" in [item["value"] for item in ctx.domain.state["ndiSources"]]
    ctx.ndi.sources_ready.emit([])
    assert ctx.domain.state["ndiStatusKind"] == "empty"
    ctx.ndi.error.emit("NDI runtime unavailable")
    assert ctx.domain.state["ndiBusy"] is False
    assert ctx.domain.state["ndiStatusKind"] == "error"
    assert ctx.domain.state["ndiStatus"] == "NDI runtime unavailable"


def test_shortcut_editor_validates_captures_saves_and_preserves_enabled(setup_domain):
    ctx = setup_domain
    ctx.domain.invoke("addShortcut")
    ctx.domain.invoke("saveShortcut")
    assert ctx.keys.actions() == []
    assert ctx.domain.state["shortcutError"]
    ctx.domain.captureShortcut(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier.value)
    assert ctx.domain.state["shortcutSequence"] == ""
    ctx.domain.captureShortcut(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier.value)
    assert ctx.domain.state["shortcutSequence"] == "Ctrl+S"
    ctx.domain.setValue("shortcutEvent", AUTO_KEY_EVENTS[2])
    ctx.domain.setValue("shortcutEnabled", False)
    ctx.domain.refresh_language()
    assert ctx.domain.state["shortcutSequence"] == "Ctrl+S"
    ctx.domain.invoke("saveShortcut")
    action, = ctx.keys.actions()
    assert action.event == AUTO_KEY_EVENTS[2]
    assert action.sequence == "Ctrl+S"
    assert action.enabled is False
    assert ctx.domain.state["shortcutEditorOpen"] is False
    ctx.domain.invoke(f"editShortcut:{action.id}")
    assert ctx.domain.state["shortcutEnabled"] is False
    ctx.domain.setValue("shortcutSequence", "Alt+X")
    ctx.domain.invoke("cancelShortcut")
    assert ctx.keys.actions()[0] == action
    ctx.domain.invoke(f"deleteShortcut:{action.id}")
    assert ctx.keys.actions() == []


def test_unsolicited_discovery_cannot_configure_a_new_profile(setup_domain):
    ctx = setup_domain
    ctx.ndi.sources_ready.emit(["Previous profile request"])
    assert ctx.obs.ndi_source() == ""
    ctx.domain.invoke("findNdiSources")
    ctx.ndi.sources_ready.emit(["Chosen program"])
    assert ctx.obs.ndi_source() == "Chosen program"
    assert ctx.domain.state["ndiSource"] == "Chosen program"


def test_nested_ndi_keeps_configuration_when_obs_disabled_and_discovery_needs_no_websocket(
    setup_domain,
):
    ctx = setup_domain
    changes = []
    ctx.domain.obs_stream_config_changed.connect(lambda: changes.append(ctx.obs.ndi_enabled()))
    ctx.service.state_changed.emit(OBSConnectionState.DISCONNECTED, "")
    ctx.domain.setValue("ndiEnabled", True)
    assert ctx.ndi.calls == 1
    assert ctx.domain.state["ndiBusy"] is True
    assert changes == [True]
    ctx.ndi.sources_ready.emit(["OBS program"])
    assert ctx.domain.state["ndiStatusKind"] == "success"
    assert ctx.obs.ndi_source() == "OBS program"
    ctx.domain.setValue("obsEnabled", False)
    assert ctx.obs.ndi_enabled() is True
    assert ctx.obs.ndi_source() == "OBS program"
    assert ctx.domain.state["ndiEnabled"] is True
    assert ctx.domain.state["ndiSource"] == "OBS program"
    ctx.domain.setValue("obsEnabled", True)
    assert ctx.ndi.calls == 1
    assert ctx.domain.state["ndiStatusKind"] == "success"
    ctx.domain.setValue("ndiEnabled", False)
    assert changes[-1] is False
    assert ctx.obs.ndi_source() == "OBS program"


def test_comma_shortcut_roundtrips_through_editor_and_store(setup_domain):
    ctx = setup_domain
    ctx.domain.invoke("addShortcut")
    ctx.domain.captureShortcut(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier.value)
    ctx.domain.invoke("saveShortcut")
    assert ctx.keys.actions()[0].sequence == "Ctrl+,"


def test_editing_existing_shortcut_keeps_identity_and_order(setup_domain):
    ctx = setup_domain
    first = AutoKeyAction("a", AUTO_KEY_EVENTS[0], "Alt+S")
    second = AutoKeyAction("b", AUTO_KEY_EVENTS[1], "Alt+T")
    ctx.keys.save_actions([first, second])
    ctx.domain.invoke("editShortcut:a")
    ctx.domain.setValue("shortcutSequence", "Ctrl+Shift+X")
    ctx.domain.invoke("saveShortcut")
    assert [item.id for item in ctx.keys.actions()] == ["a", "b"]
    assert ctx.keys.actions()[0].sequence == "Ctrl+Shift+X"
    assert ctx.keys.actions()[1] == second


def test_share_hotkey_uses_same_canonical_capture_without_creating_action(setup_domain):
    ctx = setup_domain
    ctx.domain.invoke("editShareHotkey")
    ctx.domain.setValue("shortcutSequence", "ctrl+shift+s")
    ctx.domain.invoke("saveShortcut")
    assert ctx.share.hotkey() == "Ctrl+Shift+S"
    assert ctx.keys.actions() == []
    assert ctx.domain.state["shareHotkey"] == "Ctrl+Shift+S"


def test_cleanup_cancels_pending_saves_and_ignores_late_service_results(setup_domain):
    ctx = setup_domain
    ctx.domain.setValue("obsPassword", "must-not-save")
    ctx.domain.invoke("addShortcut")
    ctx.domain.cleanup()
    ctx.domain.cleanup()
    assert ctx.domain.state["obsPassword"] == ""
    assert ctx.domain.state["shortcutEditorOpen"] is False
    assert not ctx.domain._obs_save_timer.isActive()
    ctx.ndi.sources_ready.emit(["Late source"])
    ctx.service.state_changed.emit(OBSConnectionState.ERROR, "Late error")
    ctx.domain._on_ndi_sources(["Queued source"])
    ctx.domain._save_obs_connection()
    assert ctx.obs.password() == "saved-secret"
    assert ctx.obs.ndi_source() == ""
    assert ctx.domain.state["obsConnected"] is True


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_platform_permissions_and_zoom_initialization_once(setup_domain, monkeypatch, platform):
    ctx = setup_domain
    ctx.domain.cleanup()
    ctx.zoom.set_show_participants(False)
    monkeypatch.setattr(integrations.sys, "platform", platform)
    domain = IntegrationSettings(
        obs_service=None, ndi_service=None, obs_settings=ctx.obs,
        zoom_settings=ctx.zoom, auto_share_settings=ctx.share,
        auto_key_settings=ctx.keys, auto_share_accessibility_trusted=lambda: False,
        target_picker_factory=lambda **_kwargs: None,
    )
    try:
        assert domain.state["zoomAvailable"] is (platform == "win32")
        assert "cameraAvailable" not in domain.state
        assert "cameraEnabled" not in domain.state
        assert domain.state["accessibilityRequired"] is (platform == "darwin")
        assert domain.state["accessibilityTrusted"] is (platform != "darwin")
        assert ctx.zoom.show_participants() is (platform == "win32")
        ctx.zoom.set_show_participants(False)
        domain.refresh_language()
        domain.refresh_runtime()
        assert ctx.zoom.show_participants() is False
        domain.invoke("findNdiSources")
        assert domain.state["ndiStatusKind"] == "error"
    finally:
        domain.cleanup()


@pytest.fixture
def dialog_window(setup_domain):
    configure_qml_controls_style()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("settingsIntegrations", setup_domain.domain)
    engine.rootContext().setContextProperty("appTheme", current_qml_theme())
    errors = []
    engine.warnings.connect(lambda warnings: errors.extend(item.toString() for item in warnings))
    engine.loadData(
        b'import QtQuick; import QtQuick.Controls; '
        b'ApplicationWindow { width: 360; height: 440; visible: true; SettingsShortcutDialog {} }',
        QUrl.fromLocalFile(str(Path("src/solin/qml/ShortcutTest.qml").resolve())),
    )
    QApplication.processEvents()
    assert engine.rootObjects(), errors
    window = engine.rootObjects()[0]
    yield window
    window.close()
    engine.deleteLater()
    QApplication.processEvents()
    assert errors == []


def test_qml_shortcut_dialog_captures_shortcut_override_and_escape_cancels_recording(
    setup_domain, dialog_window,
):
    ctx = setup_domain
    window = dialog_window
    dialog = window.findChild(QObject, "settingsShortcutDialog")
    capture = window.findChild(QObject, "settingsShortcutCapture")
    ctx.domain.invoke("addShortcut")
    QTest.qWait(30)
    assert dialog.property("visible") is True
    assert dialog.property("width") <= 336
    assert dialog.property("height") <= 416
    QMetaObject.invokeMethod(capture, "clicked")
    QTest.qWait(10)
    QTest.keyClick(window, Qt.Key.Key_Control)
    assert dialog.property("recording") is True
    QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert ctx.domain.state["shortcutSequence"] == "Ctrl+F"
    assert dialog.property("recording") is False
    QMetaObject.invokeMethod(capture, "clicked")
    QTest.qWait(10)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert dialog.property("recording") is False
    assert dialog.property("visible") is True
    assert ctx.domain.state["shortcutSequence"] == "Ctrl+F"
    ctx.domain.invoke("saveShortcut")
    QTest.qWait(30)
    assert dialog.property("visible") is False
    assert ctx.keys.actions()[0].sequence == "Ctrl+F"


def test_qml_shortcut_dialog_can_cancel_and_reopen_in_share_mode(setup_domain, dialog_window):
    ctx = setup_domain
    dialog = dialog_window.findChild(QObject, "settingsShortcutDialog")
    ctx.domain.invoke("addShortcut")
    QTest.qWait(30)
    QTest.keyClick(dialog_window, Qt.Key.Key_Escape)
    QTest.qWait(30)
    assert ctx.domain.state["shortcutEditorOpen"] is False
    ctx.domain.invoke("editShareHotkey")
    QTest.qWait(30)
    assert dialog.property("visible") is True
    assert not dialog_window.findChild(QObject, "settingsShortcutEvent").property("visible")
    assert not dialog_window.findChild(QObject, "settingsShortcutEnabled").property("visible")
    cancel = dialog_window.findChild(QObject, "settingsShortcutCancel")
    QMetaObject.invokeMethod(cancel, "clicked")
    QTest.qWait(30)
    assert dialog.property("visible") is False
    assert ctx.share.hotkey() == ""
