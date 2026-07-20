"""Framework-independent configuration for meeting-aware media countdowns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .models import MediaCountdownPresentation

DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS = 10 * 60
MIN_MEDIA_COUNTDOWN_LEAD_SECONDS = 10
MAX_MEDIA_COUNTDOWN_LEAD_SECONDS = 2 * 60 * 60


def clamp_media_countdown_lead_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS
    return max(
        MIN_MEDIA_COUNTDOWN_LEAD_SECONDS,
        min(MAX_MEDIA_COUNTDOWN_LEAD_SECONDS, seconds),
    )


def normalize_media_countdown_presentation(value: Any) -> MediaCountdownPresentation:
    if isinstance(value, MediaCountdownPresentation):
        return value
    try:
        return MediaCountdownPresentation(str(value))
    except ValueError:
        return MediaCountdownPresentation.CIRCULAR


@dataclass(frozen=True, slots=True)
class MediaCountdownAutomationConfig:
    enabled: bool = False
    lead_seconds: int = DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS
    presentation: MediaCountdownPresentation = MediaCountdownPresentation.CIRCULAR

    def normalized(self) -> "MediaCountdownAutomationConfig":
        return MediaCountdownAutomationConfig(
            enabled=bool(self.enabled),
            lead_seconds=clamp_media_countdown_lead_seconds(self.lead_seconds),
            presentation=normalize_media_countdown_presentation(self.presentation),
        )


class MediaCountdownAutomationStatus(str, Enum):
    DISABLED = "disabled"
    SCHEDULE_REQUIRED = "schedule_required"
    READY = "ready"
    WAITING_FOR_PROJECTION = "waiting_for_projection"
    ACTIVE = "active"
    SUPPRESSED = "suppressed"
    MISSED = "missed"


class MediaCountdownBlockingReason(str, Enum):
    NONE = ""
    NO_WINDOW = "no_window"
    IN_USE = "in_use"
    AUTOMATION_UNAVAILABLE = "automation_unavailable"


__all__ = [
    "DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS",
    "MAX_MEDIA_COUNTDOWN_LEAD_SECONDS",
    "MIN_MEDIA_COUNTDOWN_LEAD_SECONDS",
    "MediaCountdownAutomationConfig",
    "MediaCountdownBlockingReason",
    "MediaCountdownAutomationStatus",
    "clamp_media_countdown_lead_seconds",
    "normalize_media_countdown_presentation",
]
