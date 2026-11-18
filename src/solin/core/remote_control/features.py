"""Feature flags governing local remote-control capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


TIMER_REMOTE_REQUIRE_CREDENTIALS: Final[bool] = True
TIMER_REMOTE_ALLOW_SCHEDULE_STRUCTURE_EDITS: Final[bool] = True


@dataclass(frozen=True, slots=True)
class TimerRemoteFeatureFlags:
    """Immutable feature-flag configuration for the remote meeting timer."""

    require_credentials: bool = TIMER_REMOTE_REQUIRE_CREDENTIALS
    allow_schedule_structure_edits: bool = TIMER_REMOTE_ALLOW_SCHEDULE_STRUCTURE_EDITS

    def __post_init__(self) -> None:
        if not isinstance(self.require_credentials, bool):
            raise TypeError("require_credentials must be a boolean")
        if not isinstance(self.allow_schedule_structure_edits, bool):
            raise TypeError("allow_schedule_structure_edits must be a boolean")


DEFAULT_TIMER_REMOTE_FEATURE_FLAGS: Final = TimerRemoteFeatureFlags()
