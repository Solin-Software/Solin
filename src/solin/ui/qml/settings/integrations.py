"""Reactive settings for external integrations and projection automations."""

from __future__ import annotations

import sys
import uuid
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence

from solin.core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE
from solin.core.integrations.automation.auto_key_actions import AUTO_KEY_EVENTS, AutoKeyAction
from solin.core.integrations.automation.obs import OBSConnectionState
from solin.core.integrations.automation.settings import (
    AutoKeySettingsStore,
    AutoShareSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from solin.ui.auto_key_labels import auto_key_event_label
from solin.ui.obs_status_text import translated_obs_status_text

from .domain import SettingsDomain


def _options(values: list[str]) -> list[dict[str, str]]:
    return [{"value": value, "label": value} for value in dict.fromkeys(values) if value]


class IntegrationSettings(SettingsDomain):
    """Services are wired eagerly; no QML page must exist to receive their state."""

    zoom_enabled_toggled = Signal(bool)
    zoom_participants_toggled = Signal(bool)
    obs_stream_config_changed = Signal()

    def __init__(
        self,
        *,
        obs_service: Any,
        ndi_service: Any,
        obs_settings: OBSSettingsStore,
        zoom_settings: ZoomSettingsStore,
        auto_share_settings: AutoShareSettingsStore,
        auto_key_settings: AutoKeySettingsStore,
        auto_share_accessibility_trusted: Callable[[], bool],
        target_picker_factory: Callable[..., Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._obs = obs_service
        self._ndi = ndi_service
        self._obs_settings = obs_settings
        self._zoom_settings = zoom_settings
        self._auto_share_settings = auto_share_settings
        self._auto_key_settings = auto_key_settings
        self._accessibility_trusted = auto_share_accessibility_trusted
        self._target_picker_factory = target_picker_factory
        self._closed = False
        self._overlay = None
        self._connections: list[tuple[Any, Callable]] = []
        self._obs_message = ""
        self._obs_state = OBSConnectionState.DISCONNECTED
        self._scenes: list[str] = []
        self._ndi_sources: list[str] = []
        self._ndi_result_received = False
        self._ndi_error_message = ""
        self._password_edited = False
        self._obs_save_timer = QTimer(self)
        self._obs_save_timer.setSingleShot(True)
        self._obs_save_timer.setInterval(800)
        self._obs_save_timer.timeout.connect(self._save_obs_connection)
        self._permission_timer = QTimer(self)
        self._permission_timer.setSingleShot(True)
        self._permission_timer.setInterval(1000)
        self._permission_timer.timeout.connect(self.refresh_runtime)

        # Zoom attendance is always part of this integration. Initialize once,
        # independently of lazy page creation, theme changes or retranslations.
        if sys.platform == "win32":
            self._zoom_settings.set_show_participants(True)
        self.publish(
            obsEnabled=obs_settings.is_enabled(),
            obsPort=obs_settings.raw_websocket_port(), obsPassword="",
            obsConnectionError="", obsConnectionFeedback="",
            obsDefaultScene=obs_settings.default_scene(),
            obsMediaScene=obs_settings.media_window_scene(),
            ndiEnabled=obs_settings.ndi_enabled(), ndiSource=obs_settings.ndi_source(),
            ndiBusy=False, ndiStatus="", ndiStatusKind="",
            zoomAvailable=sys.platform == "win32", zoomEnabled=zoom_settings.is_enabled(),
            zoomParticipants=zoom_settings.show_participants(),
            autoShareEnabled=auto_share_settings.is_enabled(),
            shareHotkey=auto_share_settings.ensure_hotkey(), shareTargetBusy=False,
            autoKeysEnabled=auto_key_settings.is_enabled(),
            shortcutEditorOpen=False, shortcutShareMode=False, shortcutId="",
            shortcutEvent=AUTO_KEY_EVENTS[0], shortcutSequence="", shortcutEnabled=True,
            shortcutError="",
        )
        if self._obs is not None:
            self._connect(self._obs.state_changed, self._on_obs_state)
            self._connect(self._obs.scenes_updated, self._on_obs_scenes)
            self._obs_state = self._obs.state
            self._scenes = list(self._obs.scenes)
        if self._ndi is not None:
            self._connect(self._ndi.sources_ready, self._on_ndi_sources)
            self._connect(self._ndi.error, self._on_ndi_error)
        self.refresh_language()

    def _connect(self, signal: Any, callback: Callable) -> None:
        signal.connect(callback)
        self._connections.append((signal, callback))

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802
        if self._closed:
            return
        try:
            self._set_value(key, value)
        except (ValueError, OSError, RuntimeError) as error:
            self.fail(str(error))

    def _set_value(self, key: str, value: Any) -> None:
        if key in {"obsPort", "obsPassword"}:
            self.publish(**{key: str(value)}, obsConnectionError="", obsConnectionFeedback="")
            if key == "obsPassword":
                self._password_edited = True
            self._obs_save_timer.start()
            return
        if key in {"shortcutEvent", "shortcutSequence", "shortcutEnabled"}:
            if not self._state["shortcutEditorOpen"]:
                return
            if key == "shortcutEvent" and value not in AUTO_KEY_EVENTS:
                return
            if key == "shortcutSequence":
                value = self._canonical_sequence(str(value))
            self.publish(**{key: bool(value) if key == "shortcutEnabled" else value},
                         shortcutError="")
            return
        if key == "obsEnabled":
            self._obs_settings.set_enabled(bool(value))
            if self._obs is not None:
                self._obs.start() if value else self._obs.stop()
        elif key == "ndiEnabled":
            self._obs_settings.set_ndi_enabled(bool(value))
            if value:
                self._find_ndi_sources()
            self.obs_stream_config_changed.emit()
        elif key == "ndiSource":
            if not value or value not in [item["value"] for item in self._state["ndiSources"]]:
                return
            self._obs_settings.set_ndi_source(str(value))
            self.obs_stream_config_changed.emit()
        elif key in {"obsDefaultScene", "obsMediaScene"}:
            if value and value not in [item["value"] for item in self._state["obsScenes"]]:
                return
            default = str(value) if key == "obsDefaultScene" else self._state["obsDefaultScene"]
            media = str(value) if key == "obsMediaScene" else self._state["obsMediaScene"]
            self._obs_settings.set_scenes(default, media)
        elif key == "zoomEnabled" and self._state["zoomAvailable"]:
            self._zoom_settings.set_enabled(bool(value))
            self.zoom_enabled_toggled.emit(bool(value))
        elif key == "zoomParticipants" and self._state["zoomAvailable"]:
            self._zoom_settings.set_show_participants(bool(value))
            self.zoom_participants_toggled.emit(bool(value))
        elif key == "autoShareEnabled":
            self._auto_share_settings.set_enabled(bool(value))
        elif key == "autoKeysEnabled":
            self._auto_key_settings.set_enabled(bool(value))
        else:
            raise ValueError(f"Unknown or unavailable integration field: {key}")
        self.publish(**{key: value}, feedback="", feedbackKind="")
        self.refresh_runtime()

    @Slot(str)
    def invoke(self, action: str) -> None:
        if self._closed:
            return
        actions = {
            "findNdiSources": self._find_ndi_sources,
            "configureShareTarget": self._configure_share_target,
            "openAccessibility": self._open_accessibility,
            "addShortcut": self._open_shortcut,
            "editShareHotkey": lambda: self._open_shortcut(share=True),
            "saveShortcut": self._save_shortcut,
            "cancelShortcut": self._cancel_shortcut,
        }
        try:
            if action.startswith("editShortcut:"):
                self._open_shortcut(action.split(":", 1)[1])
            elif action.startswith("deleteShortcut:"):
                action_id = action.split(":", 1)[1]
                self._auto_key_settings.save_actions([
                    item for item in self._auto_key_settings.actions() if item.id != action_id
                ])
                if self._state["shortcutId"] == action_id:
                    self._cancel_shortcut()
                self._refresh_shortcuts()
            elif action in actions:
                actions[action]()
            else:
                raise ValueError(f"Unknown integration action: {action}")
        except (ValueError, OSError, RuntimeError) as error:
            self.fail(str(error))

    def _save_obs_connection(self) -> None:
        if self._closed:
            return
        try:
            text = self._state["obsPort"].strip()
            port = int(text) if text else 0
            if port < 0 or port > 65535:
                raise ValueError
        except ValueError:
            self.publish(obsConnectionError=QCoreApplication.translate(
                "SettingsWidget", "Enter a valid port (1–65535)."
            ))
            return
        password = (self._state["obsPassword"] if self._password_edited
                    else self._obs_settings.password())
        try:
            self._obs_settings.set_connection(port, password)
        except (ValueError, OSError, RuntimeError) as error:
            self.publish(obsConnectionError=str(error))
            return
        self._password_edited = False
        self.publish(
            obsPassword="",
            obsConnectionError="",
            obsConnectionFeedback=QCoreApplication.translate(
                "SettingsWidget", "✓ Configuration saved — reconnecting…"
            ),
        )
        if self._obs is not None and self._obs_settings.is_enabled():
            self._obs.stop()
            if port > 0:
                self._obs.start()

    def _on_obs_state(self, state: OBSConnectionState, message: str) -> None:
        if self._closed:
            return
        self._obs_state = state
        self._obs_message = message or ""
        self._refresh_obs()

    def _on_obs_scenes(self, scenes: list) -> None:
        if self._closed:
            return
        self._scenes = list(scenes)
        self._refresh_obs()

    def _refresh_obs(self) -> None:
        scenes = _options(self._scenes + [self._obs_settings.default_scene(),
                                         self._obs_settings.media_window_scene()])
        self.publish(
            obsState=self._obs_state.name.lower(),
            obsStatus=translated_obs_status_text(self._obs_state, self._obs_message),
            obsConnected=self._obs_state == OBSConnectionState.CONNECTED,
            obsScenes=[{
                "value": "",
                "label": QCoreApplication.translate("SettingsWidget", "— Select scene —"),
            }] + scenes,
            obsMediaSceneEnabled=(self._obs_state == OBSConnectionState.CONNECTED
                                  and (MEMORIZE_PRE_MEDIA_SCENE
                                       or bool(self._obs_settings.default_scene()))),
        )

    def _find_ndi_sources(self) -> None:
        if self._state["ndiBusy"]:
            return
        if self._ndi is None:
            self._on_ndi_error(QCoreApplication.translate(
                "SettingsWidget", "NDI receiver is not available."
            ))
            return
        self._ndi_error_message = ""
        self.publish(
            ndiBusy=True,
            ndiStatus=QCoreApplication.translate(
                "SettingsWidget", "Looking for NDI sources on this network."
            ),
            ndiStatusKind="pending",
        )
        try:
            self._ndi.refresh_sources()
        except (ValueError, OSError, RuntimeError) as error:
            self._on_ndi_error(str(error))

    def _on_ndi_sources(self, sources: list) -> None:
        if self._closed:
            return
        self._ndi_result_received = True
        self._ndi_error_message = ""
        self._ndi_sources = list(dict.fromkeys(source for source in sources if source))
        # Discovery must not replace a user's stored source when it is offline.
        if (self._state["ndiBusy"] and self._ndi_sources
                and not self._obs_settings.ndi_source()):
            self._obs_settings.set_ndi_source(self._ndi_sources[0])
            self.publish(ndiSource=self._ndi_sources[0])
            self.obs_stream_config_changed.emit()
        self.publish(ndiBusy=False)
        self._refresh_ndi()

    def _on_ndi_error(self, message: str) -> None:
        if self._closed:
            return
        self._ndi_error_message = message
        self.publish(ndiBusy=False, ndiStatus=message, ndiStatusKind="error")

    def _refresh_ndi(self) -> None:
        self.publish(ndiSources=_options(self._ndi_sources + [self._obs_settings.ndi_source()]))
        if self._state["ndiBusy"]:
            self.publish(ndiStatus=QCoreApplication.translate(
                "SettingsWidget", "Looking for NDI sources on this network."
            ))
        elif self._ndi_error_message:
            self.publish(ndiStatus=self._ndi_error_message, ndiStatusKind="error")
        elif self._ndi_result_received:
            self.publish(
                ndiStatus=(
                    self.tr("%n NDI source found.", None, len(self._ndi_sources))
                    if self._ndi_sources else QCoreApplication.translate(
                        "SettingsWidget",
                        "No NDI sources found. Check that DistroAV Main Output is enabled in OBS.",
                    )
                ),
                ndiStatusKind="success" if self._ndi_sources else "empty",
            )

    @staticmethod
    def _canonical_sequence(text: str) -> str:
        sequence = QKeySequence.fromString(text, QKeySequence.SequenceFormat.PortableText)
        if sequence.isEmpty() or sequence[0].key() == Qt.Key.Key_unknown:
            return ""
        return QKeySequence(sequence[0]).toString(QKeySequence.SequenceFormat.PortableText)

    @Slot(int, int)
    def captureShortcut(self, key: int, modifiers: int) -> None:  # noqa: N802
        if self._closed or not self._state["shortcutEditorOpen"]:
            return
        if key in {Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                   Qt.Key.Key_Meta, Qt.Key.Key_AltGr, Qt.Key.Key_unknown}:
            return
        mask = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier).value
        sequence = QKeySequence(key | (modifiers & mask))
        self.setValue("shortcutSequence", sequence.toString(QKeySequence.SequenceFormat.PortableText))

    def _open_shortcut(self, action_id: str = "", *, share: bool = False) -> None:
        action = next((item for item in self._auto_key_settings.actions()
                       if item.id == action_id), None)
        if action_id and action is None:
            return
        self.publish(
            shortcutEditorOpen=True, shortcutShareMode=share, shortcutId=action_id,
            shortcutEvent=action.event if action else AUTO_KEY_EVENTS[0],
            shortcutSequence=(self._auto_share_settings.hotkey() if share
                              else action.sequence if action else ""),
            shortcutEnabled=action.enabled if action else True, shortcutError="",
        )

    def _cancel_shortcut(self) -> None:
        self.publish(shortcutEditorOpen=False, shortcutId="", shortcutSequence="", shortcutError="")

    def _save_shortcut(self) -> None:
        if not self._state["shortcutEditorOpen"]:
            return
        sequence = self._canonical_sequence(self._state["shortcutSequence"])
        if not sequence:
            self.publish(shortcutError=QCoreApplication.translate(
                "SettingsWidget", "Press a shortcut before saving."
            ))
            return
        try:
            if self._state["shortcutShareMode"]:
                self._auto_share_settings.set_hotkey(sequence)
                self.publish(shareHotkey=sequence)
            else:
                action_id = self._state["shortcutId"]
                actions = self._auto_key_settings.actions()
                if action_id and not any(item.id == action_id for item in actions):
                    self._cancel_shortcut()
                    self._refresh_shortcuts()
                    return
                action = AutoKeyAction(action_id or uuid.uuid4().hex,
                                       self._state["shortcutEvent"], sequence,
                                       self._state["shortcutEnabled"])
                updated = [action if item.id == action_id else item for item in actions]
                if not action_id:
                    updated.append(action)
                self._auto_key_settings.save_actions(updated)
        except (ValueError, OSError, RuntimeError) as error:
            self.publish(shortcutError=str(error))
            return
        self._cancel_shortcut()
        self._refresh_shortcuts()

    def _refresh_shortcuts(self) -> None:
        self.publish(shortcuts=[dict(item.to_dict(), eventLabel=auto_key_event_label(item.event))
                                for item in self._auto_key_settings.actions()],
                     shortcutEvents=[{"value": event, "label": auto_key_event_label(event)}
                                     for event in AUTO_KEY_EVENTS])

    def _configure_share_target(self) -> None:
        if self._overlay is not None:
            self._overlay.raise_()
            self._overlay.activateWindow()
            return
        x, y = self._auto_share_settings.click_position()
        self._overlay = self._target_picker_factory(current_x=x, current_y=y, parent=None)
        self._overlay.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._overlay.position_picked.connect(self._on_share_position)
        self._overlay.destroyed.connect(self._on_overlay_closed)
        self.publish(shareTargetBusy=True)
        self._overlay.show_overlay()

    def _on_share_position(self, x: int, y: int) -> None:
        if self._closed:
            return
        try:
            self._auto_share_settings.set_click_position(x, y)
        except (ValueError, OSError, RuntimeError) as error:
            self.fail(str(error))
            return
        self.refresh_runtime()

    def _on_overlay_closed(self) -> None:
        self._overlay = None
        if not self._closed:
            self.publish(shareTargetBusy=False)

    def _open_accessibility(self) -> None:
        if sys.platform != "darwin":
            return
        QDesktopServices.openUrl(QUrl(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))
        self._permission_timer.start()

    def refresh_runtime(self) -> None:
        if self._closed:
            return
        required = sys.platform == "darwin"
        trusted = bool(self._accessibility_trusted()) if required else True
        configured = self._auto_share_settings.has_click_position()
        self.publish(
            shareTargetConfigured=configured,
            shareTargetStatus=(
                QCoreApplication.translate("SettingsWidget", "Configured")
                if configured else
                QCoreApplication.translate("SettingsWidget", "Not configured")
            ),
            accessibilityRequired=required, accessibilityTrusted=trusted,
            accessibilityStatus=(
                QCoreApplication.translate(
                    "SettingsWidget", "Solin can send the automatic click."
                ) if trusted else QCoreApplication.translate(
                    "SettingsWidget",
                    "Allow Solin in macOS Accessibility so automatic clicks can work.",
                )
            ),
        )
        self._refresh_obs()

    def refresh_language(self) -> None:
        if self._closed:
            return
        self.refresh_runtime()
        self._refresh_ndi()
        self._refresh_shortcuts()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._obs_save_timer.stop()
        self._permission_timer.stop()
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()
        if self._overlay is not None:
            self._overlay.close()
            self._overlay = None
        self.publish(obsPassword="", shortcutSequence="", shortcutEditorOpen=False)
