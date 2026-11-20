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


class RaisedHandsStatus(StrEnum):
    INACTIVE = "inactive"
    NO_MEETING = "no_meeting"
    PANEL_CLOSED = "panel_closed"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    READY = "ready"


class ParticipantAction(StrEnum):
    LOWER_HAND = "lower_hand"
    REQUEST_UNMUTE = "request_unmute"
    MUTE_AND_LOWER = "mute_and_lower"

    @property
    def steps(self) -> tuple[str, ...]:
        return ("mute", "lower_hand") if self is self.MUTE_AND_LOWER else (self.value,)


class ParticipantStepStatus(StrEnum):
    CHANGED = "changed"
    ALREADY_SET = "already_set"
    REQUESTED = "requested"


@dataclass(frozen=True, slots=True)
class ParticipantStepResult:
    step: str
    status: ParticipantStepStatus


@dataclass(frozen=True, slots=True)
class RaisedHand:
    name: str
    audio: AudioState
    reference: str | None


@dataclass(frozen=True, slots=True)
class RaisedHandsSnapshot:
    status: RaisedHandsStatus = RaisedHandsStatus.INACTIVE
    hands: tuple[RaisedHand, ...] = ()
    chronological: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ParticipantActionResult:
    action: ParticipantAction
    reference: str
    completed_steps: tuple[ParticipantStepResult, ...] = ()
    failure: StepFailure | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None and not self.cancelled


class ZoomAutomationError(RuntimeError):
    """Stable error code from the native automation boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
