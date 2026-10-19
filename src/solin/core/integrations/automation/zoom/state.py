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


@dataclass(frozen=True, slots=True)
class MeetingState:
    audio: AudioState = AudioState.UNKNOWN
    video: VideoState = VideoState.UNKNOWN
    in_meeting: bool = False


@dataclass(frozen=True, slots=True)
class StepFailure:
    step: str
    code: str


@dataclass(frozen=True, slots=True)
class SequenceResult:
    sequence: str
    failed_steps: tuple[StepFailure, ...] = ()
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.cancelled and not self.failed_steps


class ZoomAutomationError(RuntimeError):
    """Stable error code from the native automation boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
