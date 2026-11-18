"""Hosted-meeting settings (per profile) and the meeting server.

The **meeting server** (the hub, :data:`DEFAULT_SERVER` unless the congregation runs its
own) gives meetings their public address, ``https://<server>/<code>``. It is open to
every Solin: no account or connection code. Empty means no server: meetings are reached
on this network only.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.integrations.automation.settings import _ProfilePrefsSettings

from .links import LinkMode, LinksState

DEFAULT_SERVER = "meet.solin.run"
DEFAULT_WEB_PORT = 8780
# Before the hub was open, it was configured with a connection code; its domain is kept.
_OLD_CONNECTION_CODE = "solin-hub:"
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*(:\d{1,5})?$")


class MeetingProvider(StrEnum):
    ZOOM = "zoom"
    SOLIN = "solin"


class MeetingQuality(StrEnum):
    AUDIO = "audio"
    SD = "360p"
    HD = "720p"

    @property
    def video(self) -> tuple[int, int, int] | None:
        """(width, height, kbps), or None for audio only."""

        return {MeetingQuality.SD: (640, 360, 450), MeetingQuality.HD: (1280, 720, 1700)}.get(self)


@dataclass(frozen=True, slots=True)
class HubServer:
    """The meeting server: its address is the meetings' public origin."""

    domain: str

    @property
    def public_origin(self) -> str:
        return f"https://{self.domain}"


def clean_server(text: str) -> str | None:
    """A server address as typed ("meet.solin.run", "https://meet.solin.run/"), or None if invalid."""

    host = (text or "").strip().lower()
    host = re.sub(r"^https?://", "", host).split("/", 1)[0]
    return host if _HOST.match(host) else None


def _server_from_old_code(code: str) -> str:
    payload = code[len(_OLD_CONNECTION_CODE):]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return ""
    return clean_server(str(data.get("domain") or "")) or "" if isinstance(data, dict) else ""


@dataclass(frozen=True, slots=True)
class HostedMeetingSettingsStore(_ProfilePrefsSettings):
    def provider(self) -> MeetingProvider:
        try:
            return MeetingProvider(self.settings.string(SettingsKey.HOSTED_MEETING_PROVIDER, MeetingProvider.ZOOM))
        except ValueError:
            return MeetingProvider.ZOOM

    def set_provider(self, provider: MeetingProvider) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_PROVIDER, MeetingProvider(provider).value)

    def hall_name(self) -> str:
        return self.settings.string(SettingsKey.HOSTED_MEETING_HALL_NAME, "")

    def set_hall_name(self, name: str) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_HALL_NAME, " ".join(str(name).split())[:80])

    def quality(self) -> MeetingQuality:
        try:
            return MeetingQuality(self.settings.string(SettingsKey.HOSTED_MEETING_QUALITY, MeetingQuality.SD))
        except ValueError:
            return MeetingQuality.SD

    def set_quality(self, quality: MeetingQuality) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_QUALITY, MeetingQuality(quality).value)

    def link_mode(self) -> LinkMode:
        try:
            return LinkMode(self.settings.string(SettingsKey.HOSTED_MEETING_LINK_MODE, LinkMode.SAME))
        except ValueError:
            return LinkMode.SAME

    def set_link_mode(self, mode: LinkMode) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_LINK_MODE, LinkMode(mode).value)

    def links(self) -> LinksState:
        """The permanent link and the prepared meetings (see :mod:`.links`)."""

        try:
            data = json.loads(self.settings.string(SettingsKey.HOSTED_MEETING_LINK, "") or "{}")
        except json.JSONDecodeError:
            data = {}
        return LinksState.from_json(data)

    def set_links(self, state: LinksState) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_LINK, json.dumps(state.to_json(), separators=(",", ":")))

    def devices(self) -> list[dict[str, object]]:
        """Approved stage and moderator devices (see :mod:`.devices`)."""

        try:
            data = json.loads(self.settings.string(SettingsKey.HOSTED_MEETING_DEVICES, "") or "[]")
        except json.JSONDecodeError:
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def set_devices(self, devices: list[dict[str, object]]) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_DEVICES, json.dumps(devices, separators=(",", ":")))

    def panel_docked(self) -> bool:
        """Whether the Live meeting console was left attached to the main window."""
        value = self.settings.value(SettingsKey.HOSTED_MEETING_PANEL_DOCKED, False)
        return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes"}

    def set_panel_docked(self, docked: bool) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_PANEL_DOCKED, bool(docked))

    def panel_width(self, default: int) -> int:
        """The docked console's width, as the operator last dragged it."""

        try:
            return int(self.settings.value(SettingsKey.HOSTED_MEETING_PANEL_WIDTH, default))
        except (TypeError, ValueError):
            return default

    def set_panel_width(self, width: int) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_PANEL_WIDTH, int(width))

    def _json(self, key: str) -> dict[str, str]:
        try:
            data = json.loads(self.settings.string(key, "") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def hall_feed(self) -> dict[str, str]:
        """``{"mode": "system_default"|"device"|"none", "device_id", "display_name"}``."""

        data = self._json(SettingsKey.HOSTED_MEETING_HALL_FEED)
        return {"mode": data.get("mode", "system_default"), "device_id": data.get("device_id", ""),
                "display_name": data.get("display_name", "")}

    def set_hall_feed(self, mode: str, device_id: str = "", display_name: str = "") -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_HALL_FEED, json.dumps(
            {"mode": mode, "device_id": device_id, "display_name": display_name}))

    def hall_feed_delay_ms(self) -> int:
        """Delay applied to the hall feed so the mixer's audio lines up with the camera (0-500 ms)."""

        try:
            value = int(self.settings.value(SettingsKey.HOSTED_MEETING_HALL_FEED_DELAY, 0, int))
        except (TypeError, ValueError):
            return 0
        return max(0, min(500, value))

    def set_hall_feed_delay_ms(self, value: int) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_HALL_FEED_DELAY, max(0, min(500, int(value))))

    def remote_audio_device(self) -> dict[str, str]:
        """The output device the commenters are played on (``{"id", "name"}``; empty = default)."""

        data = self._json(SettingsKey.HOSTED_MEETING_REMOTE_AUDIO_DEVICE)
        return {"id": data.get("id", ""), "name": data.get("name", "")}

    def set_remote_audio_device(self, device_id: str, name: str) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_REMOTE_AUDIO_DEVICE,
                                json.dumps({"id": device_id, "name": name}))

    def web_port(self) -> int:
        try:
            port = int(self.settings.value(SettingsKey.HOSTED_MEETING_WEB_PORT, DEFAULT_WEB_PORT, int))
        except (TypeError, ValueError):
            return DEFAULT_WEB_PORT
        return port if 1024 <= port < 65536 else DEFAULT_WEB_PORT

    def set_web_port(self, port: int) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_WEB_PORT, int(port))

    def server(self) -> str:
        """The meeting server's address; "" = this network only."""

        value = self.settings.string(SettingsKey.HOSTED_MEETING_HUB, DEFAULT_SERVER).strip()
        if value.startswith(_OLD_CONNECTION_CODE):
            return _server_from_old_code(value)
        return clean_server(value) or "" if value else ""

    def set_server(self, text: str) -> str:
        """Store a server address ("" = this network only); raises ValueError when it is not one."""

        text = (text or "").strip()
        host = clean_server(text) if text else ""
        if host is None:
            raise ValueError("not a server address")
        self.settings.set_value(SettingsKey.HOSTED_MEETING_HUB, host)
        return host

    def hub(self) -> HubServer | None:
        server = self.server()
        return HubServer(server) if server else None
