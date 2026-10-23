"""Profile preferences for meeting hosting: Zoom (automated) or Solin-hosted.

The LiveKit API key and secret are deliberately *not* stored: they are only shared between
Solin and the server it starts, so :meth:`HostedMeetingSettingsStore.meeting_settings`
generates fresh ones for every meeting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

from .meeting_core import MeetingPolicy
from .meeting_service import HallAudioSettings, MeetingSettings
from .server_config import ServerRole, ServerSettings, generate_credentials

DEFAULT_HALL_NAME = "Kingdom Hall"
MAX_HALL_NAME_LENGTH = 48


class MeetingProviderChoice(StrEnum):
    ZOOM = "zoom"
    SOLIN = "solin"


class MeetingQuality(StrEnum):
    """Program quality sent to attendees; the spike sized 360p as the default."""

    SD = "360p"
    HD = "720p"
    AUDIO_ONLY = "audio"

    @property
    def size(self) -> tuple[int, int]:
        return (1280, 720) if self is MeetingQuality.HD else (640, 360)

    @property
    def max_bitrate(self) -> int:
        return 1_700_000 if self is MeetingQuality.HD else 800_000


@dataclass(frozen=True, slots=True)
class HostedMeetingSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings) -> HostedMeetingSettingsStore:
        return cls(SettingsStore.for_namespace(profile_settings.organization, QSETTINGS_PREFS_APP))

    def provider(self) -> MeetingProviderChoice:
        value = self.settings.string(SettingsKey.MEETING_PROVIDER, MeetingProviderChoice.ZOOM)
        try:
            return MeetingProviderChoice(value)
        except ValueError:
            return MeetingProviderChoice.ZOOM

    def set_provider(self, provider: MeetingProviderChoice | str) -> None:
        self.settings.set_value(SettingsKey.MEETING_PROVIDER, MeetingProviderChoice(provider).value)

    def hall_name(self) -> str:
        name = self.settings.string(SettingsKey.MEETING_HALL_NAME, DEFAULT_HALL_NAME).strip()
        return name[:MAX_HALL_NAME_LENGTH] or DEFAULT_HALL_NAME

    def set_hall_name(self, name: str) -> None:
        self.settings.set_value(SettingsKey.MEETING_HALL_NAME, name.strip()[:MAX_HALL_NAME_LENGTH])

    def quality(self) -> MeetingQuality:
        value = self.settings.string(SettingsKey.MEETING_QUALITY, MeetingQuality.SD)
        try:
            return MeetingQuality(value)
        except ValueError:
            return MeetingQuality.SD

    def set_quality(self, quality: MeetingQuality | str) -> None:
        self.settings.set_value(SettingsKey.MEETING_QUALITY, MeetingQuality(quality).value)

    def e2ee(self) -> bool:
        return bool(self.settings.value(SettingsKey.MEETING_E2EE, True, bool))

    def set_e2ee(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.MEETING_E2EE, bool(enabled))

    def attendee_video(self) -> bool:
        return bool(self.settings.value(SettingsKey.MEETING_ATTENDEE_VIDEO, False, bool))

    def set_attendee_video(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.MEETING_ATTENDEE_VIDEO, bool(enabled))

    def hall_audio(self) -> HallAudioSettings:
        value = self.settings.value
        return HallAudioSettings(
            enabled=bool(value(SettingsKey.MEETING_HALL_AUDIO, True, bool)),
            recording_device=self.settings.string(SettingsKey.MEETING_RECORDING_DEVICE, "")[:256],
            playout_device=self.settings.string(SettingsKey.MEETING_PLAYOUT_DEVICE, "")[:256],
            echo_cancellation=bool(value(SettingsKey.MEETING_ECHO_CANCELLATION, True, bool)),
            noise_suppression=bool(value(SettingsKey.MEETING_NOISE_SUPPRESSION, True, bool)),
        )

    def set_hall_audio(self, **changes: object) -> None:
        keys = {
            "enabled": SettingsKey.MEETING_HALL_AUDIO,
            "recording_device": SettingsKey.MEETING_RECORDING_DEVICE,
            "playout_device": SettingsKey.MEETING_PLAYOUT_DEVICE,
            "echo_cancellation": SettingsKey.MEETING_ECHO_CANCELLATION,
            "noise_suppression": SettingsKey.MEETING_NOISE_SUPPRESSION,
        }
        for name, value in changes.items():
            if name not in keys:
                raise ValueError(f"Unknown hall audio setting {name!r}")
            if name.endswith("_device"):
                value = str(value)[:256]
            else:
                value = bool(value)
            self.settings.set_value(keys[name], value)

    def meeting_settings(self) -> MeetingSettings:
        """Everything a new meeting needs, with fresh server credentials."""
        key, secret = generate_credentials()
        quality = self.quality()
        width, height = quality.size
        return MeetingSettings(
            server=ServerSettings(api_key=key, api_secret=secret, role=ServerRole.HALL),
            display_name=self.hall_name(),
            width=width,
            height=height,
            audio_only=quality is MeetingQuality.AUDIO_ONLY,
            e2ee=self.e2ee(),
            policy=MeetingPolicy(attendee_video=self.attendee_video()),
            hall_audio=self.hall_audio(),
        )
