"""Hosted-meeting settings (per profile) and the hub connection code.

A hall joins a hub with a **connection code** the hub operator gives it::

    solin-hub:<base64url(JSON)>

where the JSON holds ``domain`` (halls are ``<hall>.<domain>``), ``frp_addr`` /
``frp_port`` (the tunnel server), ``frp_token``, ``hall`` / ``hall_key`` (this hall's
reserved name and key), and optionally ``turn_secret`` / ``turn_port`` /
``turn_host``. The code is stored as-is; secrets never leave the hall PC except to
the hub they belong to.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.integrations.automation.settings import _ProfilePrefsSettings

HUB_CODE_PREFIX = "solin-hub:"
DEFAULT_WEB_PORT = 8780
_HALL = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{3,63}$")
_STAGE_KEY = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def meeting_slug(hall_name: str) -> str:
    base = unicodedata.normalize("NFKD", hall_name or "meeting").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:24] or "meeting"
    return f"{base}-{secrets.token_hex(6)}"


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
class HubConnection:
    domain: str
    frp_addr: str
    frp_port: int
    frp_token: str
    hall: str
    hall_key: str
    turn_secret: str = ""
    turn_port: int = 3478
    turn_host: str = ""

    def __post_init__(self) -> None:
        if not self.domain or not self.frp_addr or not self.frp_token:
            raise ValueError("incomplete hub connection")
        if not _HALL.match(self.hall) or not self.hall_key:
            raise ValueError("invalid hall name or key")
        if not 0 < int(self.frp_port) < 65536 or not 0 < int(self.turn_port) < 65536:
            raise ValueError("invalid port")

    @property
    def public_origin(self) -> str:
        return f"https://{self.hall}.{self.domain}"

    @property
    def turn_urls(self) -> tuple[str, ...]:
        if not self.turn_secret:
            return ()
        host = self.turn_host or self.frp_addr
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return (f"turn:{host}:{self.turn_port}?transport=udp", f"turn:{host}:{self.turn_port}?transport=tcp")

    def to_code(self) -> str:
        raw = json.dumps(asdict(self), separators=(",", ":"), sort_keys=True).encode("utf-8")
        return HUB_CODE_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @classmethod
    def from_code(cls, code: str) -> HubConnection:
        code = (code or "").strip()
        if not code.startswith(HUB_CODE_PREFIX):
            raise ValueError("not a Solin hub connection code")
        payload = code[len(HUB_CODE_PREFIX):]
        try:
            data: Any = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("unreadable hub connection code") from error
        if not isinstance(data, dict):
            raise ValueError("unreadable hub connection code")
        fields = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        try:
            return cls(**{**fields, "frp_port": int(fields.get("frp_port", 7000)),
                          "turn_port": int(fields.get("turn_port", 3478))})
        except TypeError as error:
            raise ValueError("incomplete hub connection code") from error


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

    def meeting_link(self) -> tuple[str, str]:
        """This hall's permanent (slug, stage key), created on first use.

        They stay the same from meeting to meeting so the invitation can be shared in
        advance. The lobby still decides who gets in, and each meeting has its own
        Galene key.
        """
        data = self._json(SettingsKey.HOSTED_MEETING_LINK)
        slug, stage_key = data.get("slug"), data.get("stage_key")
        if isinstance(slug, str) and isinstance(stage_key, str) and _SLUG.match(slug) and _STAGE_KEY.match(stage_key):
            return slug, stage_key
        return self.new_meeting_link()

    def new_meeting_link(self) -> tuple[str, str]:
        """Replace the permanent link; the previous one stops working."""
        slug, stage_key = meeting_slug(self.hall_name()), secrets.token_urlsafe(18)
        self.settings.set_value(SettingsKey.HOSTED_MEETING_LINK, json.dumps({"slug": slug, "stage_key": stage_key}))
        return slug, stage_key

    def panel_docked(self) -> bool:
        """Whether the Live meeting console was left attached to the main window."""
        value = self.settings.value(SettingsKey.HOSTED_MEETING_PANEL_DOCKED, False)
        return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes"}

    def set_panel_docked(self, docked: bool) -> None:
        self.settings.set_value(SettingsKey.HOSTED_MEETING_PANEL_DOCKED, bool(docked))

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

    def hub(self) -> HubConnection | None:
        code = self.settings.string(SettingsKey.HOSTED_MEETING_HUB, "")
        if not code:
            return None
        try:
            return HubConnection.from_code(code)
        except ValueError:
            return None

    def set_hub_code(self, code: str) -> HubConnection | None:
        """Validate and store a connection code; an empty code disconnects the hub."""

        code = (code or "").strip()
        if not code:
            self.settings.set_value(SettingsKey.HOSTED_MEETING_HUB, "")
            return None
        hub = HubConnection.from_code(code)
        self.settings.set_value(SettingsKey.HOSTED_MEETING_HUB, code)
        return hub
