"""What Solin needs from whatever hosts the meeting: Zoom (automated) or Solin itself.

The meeting workflow (``ZoomMeetingController``, auto-key actions, the toolbar) only needs
two transitions: ``pre_meeting`` when the meeting starts (join audio, program on, lock
self-unmute) and ``post_meeting`` when it ends (program off, allow and invite unmute).
Both providers report the outcome as a :class:`SequenceResult` on ``operation_finished``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

PRE_MEETING = "pre_meeting"
POST_MEETING = "post_meeting"
SEQUENCES = frozenset({PRE_MEETING, POST_MEETING})

# Qt signals every provider exposes, with their argument types.
PROVIDER_SIGNALS = {
    "enabled_changed": (bool,),
    "connection_changed": (bool,),
    "busy_changed": (bool,),
    "operation_finished": (object,),  # SequenceResult
    "error": (str,),
}


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


@runtime_checkable
class MeetingProvider(Protocol):
    enabled_changed: Any
    connection_changed: Any
    busy_changed: Any
    operation_finished: Any
    error: Any

    @property
    def is_available(self) -> bool: ...

    @property
    def is_enabled(self) -> bool: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_busy(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None: ...

    def request_pre_meeting(self) -> bool: ...

    def request_post_meeting(self) -> bool: ...
