"""Framework-independent Zoom meeting state value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AudioState(StrEnum):
    MUTED = "muted"
    UNMUTED = "unmuted"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"

    @property
    def is_connected(self) -> bool | None:
        if self is AudioState.UNKNOWN:
            return None
        return self is not AudioState.DISCONNECTED


class VideoState(StrEnum):
    STOPPED = "stopped"
    STARTED = "started"
    UNKNOWN = "unknown"


class ShareState(StrEnum):
    SHARING = "sharing"
    NOT_SHARING = "not_sharing"
    UNKNOWN = "unknown"

    @classmethod
    def from_active(cls, active: bool) -> ShareState:
        return cls.SHARING if active else cls.NOT_SHARING

    @property
    def is_active(self) -> bool | None:
        if self is ShareState.UNKNOWN:
            return None
        return self is ShareState.SHARING


@dataclass(frozen=True, slots=True)
class MeetingState:
    audio: AudioState = AudioState.UNKNOWN
    video: VideoState = VideoState.UNKNOWN
    sharing: ShareState = ShareState.UNKNOWN
    audio_title: str = ""
    video_title: str = ""
    participant_count: int = 0
    participant_names: tuple[str, ...] = ()
    in_meeting: bool = False

    def __post_init__(self) -> None:
        if self.participant_count < 0:
            raise ValueError("participant_count cannot be negative")

    @property
    def is_sharing(self) -> bool | None:
        return self.sharing.is_active
