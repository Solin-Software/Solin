"""Presentation-independent background-song status contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BackgroundSongStatusCode(StrEnum):
    DISABLED = "disabled"
    ENABLE_IN_SETTINGS = "enable_in_settings"
    STOPPING = "stopping"
    CONFIGURE_MEETING = "configure_meeting"
    WAITING_FOR_MEETING = "waiting_for_meeting"
    STOPPED_FOR_MEETING = "stopped_for_meeting"
    STOPPED_BEFORE_MEETING = "stopped_before_meeting"
    SIGN_LANGUAGE_UNAVAILABLE = "sign_language_unavailable"
    LOADING_AUDIO = "loading_audio"
    READY = "ready"
    LOAD_FAILED = "load_failed"
    NO_AUDIO = "no_audio"
    PLAYING = "playing"
    PLAYBACK_ERROR = "playback_error"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class BackgroundSongStatus:
    code: BackgroundSongStatusCode
    detail: str = ""


__all__ = ["BackgroundSongStatus", "BackgroundSongStatusCode"]
