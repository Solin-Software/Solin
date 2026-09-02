"""Media-control value types shared by the scene engine, its IPC, and the app.

Under Fork A the sidecar decodes media (a libobs ``ffmpeg_source``) and the app
drives it over the scene-engine IPC: ``open_media`` / ``control_media`` /
``set_media_properties`` requests, and an unsolicited ``media_playback_state``
event carrying the sidecar's playback state back for the UI.

These are Qt-free and dependency-light so both the sidecar and the client can use
them. Volume and speed cross the wire as **integer percents** (100 = unity /
normal) because the IPC validators are int/bool/text only — there is no float on
the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

# Longest media path/URL accepted on the wire (mirrors the recording path bound).
MAXIMUM_MEDIA_PATH_LENGTH = 4096
# Bounds for the percent-encoded volume/speed knobs.
MAXIMUM_VOLUME_PERCENT = 1000
MINIMUM_SPEED_PERCENT = 1
MAXIMUM_SPEED_PERCENT = 1000


class MediaPlaybackState(IntEnum):
    """obs_media_state values (obs/media-io), reported by the sidecar."""

    NONE = 0
    PLAYING = 1
    OPENING = 2
    BUFFERING = 3
    PAUSED = 4
    STOPPED = 5
    ENDED = 6
    ERROR = 7


class MediaControlAction(StrEnum):
    """Transport verbs carried by a ``control_media`` request."""

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    RESTART = "restart"
    SEEK = "seek"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class MediaPlaybackNativeState:
    """A snapshot of the sidecar media source's playback state."""

    state: MediaPlaybackState
    position_ms: int
    duration_ms: int
    path: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, MediaPlaybackState):
            raise ValueError("Invalid media playback state")
        for value, name in (
            (self.position_ms, "media position"),
            (self.duration_ms, "media duration"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid {name}")
        _bounded_text(self.path, MAXIMUM_MEDIA_PATH_LENGTH, "media path")
        _bounded_text(self.error_code, 128, "media error code")


def _bounded_text(value: object, maximum: int, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Invalid {field_name}")
