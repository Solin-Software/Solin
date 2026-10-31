"""Hosted-meeting settings for Qt Quick: provider, hall, quality, audio and hub."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from solin.core.hosted_meeting.binaries import Helper, find_helper
from solin.core.hosted_meeting.settings import (
    HostedMeetingSettingsStore,
    MeetingProvider,
    MeetingQuality,
)

from .domain import SettingsDomain

log = logging.getLogger(__name__)


def _tr(text: str) -> str:
    return QCoreApplication.translate("SettingsWidget", text)


def list_playback_devices(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Playback devices as reported by ``solin-galene-bridge devices``."""

    bridge = find_helper(Helper.BRIDGE)
    if bridge is None:
        return []
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    try:
        result = subprocess.run([str(bridge), "devices"], capture_output=True, timeout=timeout,  # noqa: S603
                                check=False, creationflags=flags)
        devices = json.loads(result.stdout or b"[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        log.warning("could not list playback devices", exc_info=True)
        return []
    return [d for d in devices if isinstance(d, dict) and d.get("id")]


class HostedMeetingSettings(SettingsDomain):
    """Profile-scoped hosted-meeting configuration."""

    provider_changed = Signal(str)
    links_changed = Signal()
    _devices_loaded = Signal(object)

    def __init__(self, store: HostedMeetingSettingsStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._devices: list[dict[str, Any]] = []
        self._hub_code_draft = ""
        self._devices_loaded.connect(self._apply_devices, Qt.ConnectionType.QueuedConnection)
        self._publish_all()

    # ── state ─────────────────────────────────────────────────────────────

    def _publish_all(self) -> None:
        store = self._store
        hub = store.hub()
        missing = [helper.value for helper in Helper if find_helper(helper) is None]
        feed = store.hall_feed()
        device = store.remote_audio_device()
        self.publish(
            meetingProvider=store.provider().value,
            providerOptions=[
                {"value": MeetingProvider.ZOOM.value, "label": _tr("Zoom")},
                {"value": MeetingProvider.SOLIN.value, "label": _tr("This computer (Solin)")},
            ],
            solinHosting=store.provider() is MeetingProvider.SOLIN,
            hallName=store.hall_name(),
            quality=store.quality().value,
            qualityOptions=[
                {"value": MeetingQuality.SD.value, "label": _tr("Standard (360p, ~0.5 Mbps per attendee)")},
                {"value": MeetingQuality.HD.value, "label": _tr("High (720p, ~1.8 Mbps per attendee)")},
                {"value": MeetingQuality.AUDIO.value, "label": _tr("Audio only")},
            ],
            hallFeed=feed["mode"],
            hallFeedOptions=[
                {"value": "system_default", "label": _tr("System default input")},
                {"value": "none", "label": _tr("No hall feed")},
            ],
            hallFeedDelay=str(store.hall_feed_delay_ms()),
            remoteAudioDevice=device["id"],
            remoteAudioOptions=self._device_options(device),
            webPort=str(store.web_port()),
            hubCode="",
            hubStatus=(_tr("Connected to {hub} as {hall}").format(hub=hub.domain, hall=hub.hall)
                       if hub else _tr("Not connected: the meeting is reachable on this network only")),
            hubConnected=hub is not None,
            helpersStatus=(_tr("Missing components: {names}").format(names=", ".join(missing)) if missing
                           else _tr("All meeting components are installed")),
        )
        self.links_changed.emit()

    def _device_options(self, current: dict[str, str]) -> list[dict[str, str]]:
        options = [{"value": "", "label": _tr("System default output")}]
        seen = {""}
        for device in self._devices:
            if device["id"] in seen:
                continue
            seen.add(device["id"])
            label = device.get("name") or device["id"]
            options.append({"value": str(device["id"]), "label": str(label)})
        if current.get("id") and current["id"] not in seen:
            options.append({"value": current["id"], "label": current.get("name") or current["id"]})
        return options

    @Slot(object)
    def _apply_devices(self, devices: list[dict[str, Any]]) -> None:
        self._devices = devices
        self.publish(remoteAudioOptions=self._device_options(self._store.remote_audio_device()))

    def refresh_runtime(self) -> None:
        self._publish_all()
        threading.Thread(target=lambda: self._devices_loaded.emit(list_playback_devices()),
                         name="solin-playback-devices", daemon=True).start()

    def refresh_language(self) -> None:
        self._publish_all()

    def cleanup(self) -> None:
        pass

    # ── edits ─────────────────────────────────────────────────────────────

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        store = self._store
        if key == "meetingProvider":
            store.set_provider(MeetingProvider(str(value)))
            self.provider_changed.emit(str(value))
        elif key == "hallName":
            store.set_hall_name(str(value))
        elif key == "quality":
            store.set_quality(MeetingQuality(str(value)))
        elif key == "hallFeed":
            store.set_hall_feed("none" if value == "none" else "system_default")
        elif key == "hallFeedDelay":
            try:
                delay = int(str(value).strip() or 0)
            except ValueError:
                self.fail(_tr("The delay must be a number of milliseconds"))
                return
            store.set_hall_feed_delay_ms(delay)
        elif key == "remoteAudioDevice":
            name = next((d.get("name", "") for d in self._devices if str(d["id"]) == str(value)), "")
            store.set_remote_audio_device(str(value), str(name))
        elif key == "webPort":
            try:
                port = int(str(value).strip())
            except ValueError:
                self.fail(_tr("The port must be a number"))
                return
            if not 1024 <= port < 65536:
                self.fail(_tr("Choose a port between 1024 and 65535"))
                return
            store.set_web_port(port)
            self.publish(feedback="", feedbackKind="")
        elif key == "hubCode":
            self._hub_code_draft = str(value)
            self.publish(hubCode=self._hub_code_draft)
            return
        else:
            raise ValueError(f"Unknown settings field: {key}")
        self._publish_all()

    @Slot(str)
    def invoke(self, action: str) -> None:
        if action == "connectHub":
            try:
                hub = self._store.set_hub_code(self._hub_code_draft)
            except ValueError:
                self.fail(_tr("This is not a valid hub connection code"))
                return
            self._hub_code_draft = ""
            self._publish_all()
            if hub is not None:
                self.succeed(_tr("Hub connection saved"))
        elif action == "disconnectHub":
            self._store.set_hub_code("")
            self._publish_all()
            self.succeed(_tr("Hub connection removed"))
        elif action == "refreshDevices":
            self.refresh_runtime()
        else:
            raise ValueError(f"Unknown settings action: {action}")
