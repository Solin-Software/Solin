"""Feature flags governing local remote-control capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class TimerRemoteHandAlertPolicy(StrEnum):
    """Visibility of raised-hand indicators on remote timer displays."""

    PUBLIC_NAMES = "public_names"
    PUBLIC_COUNT = "public_count"
    AUTHENTICATED_ONLY = "authenticated_only"
    DISABLED = "disabled"


TIMER_REMOTE_REQUIRE_CREDENTIALS: Final[bool] = True
TIMER_REMOTE_ALLOW_SCHEDULE_STRUCTURE_EDITS: Final[bool] = True
TIMER_REMOTE_HAND_ALERT_POLICY: Final = TimerRemoteHandAlertPolicy.PUBLIC_NAMES


@dataclass(frozen=True, slots=True)
class TimerRemoteFeatureFlags:
    """Immutable feature-flag configuration for the remote meeting timer."""

    require_credentials: bool = TIMER_REMOTE_REQUIRE_CREDENTIALS
    allow_schedule_structure_edits: bool = TIMER_REMOTE_ALLOW_SCHEDULE_STRUCTURE_EDITS
    hand_alert_policy: TimerRemoteHandAlertPolicy = TIMER_REMOTE_HAND_ALERT_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.require_credentials, bool):
            raise TypeError("require_credentials must be a boolean")
        if not isinstance(self.allow_schedule_structure_edits, bool):
            raise TypeError("allow_schedule_structure_edits must be a boolean")
        if not isinstance(self.hand_alert_policy, TimerRemoteHandAlertPolicy):
            raise TypeError("hand_alert_policy must be a TimerRemoteHandAlertPolicy")


DEFAULT_TIMER_REMOTE_FEATURE_FLAGS: Final = TimerRemoteFeatureFlags()
