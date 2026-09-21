"""Framework-independent contracts for bounded media playback."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from .duration import TICKS_PER_SECOND


TICKS_PER_MILLISECOND = TICKS_PER_SECOND // 1_000
MIN_PLAYBACK_INTERVAL_MS = 100
MIN_PLAYBACK_INTERVAL_TICKS = MIN_PLAYBACK_INTERVAL_MS * TICKS_PER_MILLISECOND
MAX_PERSISTED_TICKS = (1 << 63) - 1


def _validated_nonnegative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    if value > MAX_PERSISTED_TICKS:
        raise ValueError(f"{field_name} exceeds the persisted integer range")
    return value


def _ticks_to_start_ms(ticks: int) -> int:
    """Round a start offset inward so playback never starts before the trim."""
    return (ticks + TICKS_PER_MILLISECOND - 1) // TICKS_PER_MILLISECOND


@dataclass(frozen=True, slots=True)
class ResolvedPlaybackRange:
    """Absolute millisecond bounds resolved against the active media duration."""

    start_ms: int
    end_ms: int
    source_duration_ms: int

    def __post_init__(self) -> None:
        start_ms = _validated_nonnegative_int(self.start_ms, field_name="start_ms")
        end_ms = _validated_nonnegative_int(self.end_ms, field_name="end_ms")
        source_duration_ms = _validated_nonnegative_int(
            self.source_duration_ms,
            field_name="source_duration_ms",
        )
        if end_ms > source_duration_ms:
            raise ValueError("end_ms must not exceed source_duration_ms")
        if end_ms - start_ms < MIN_PLAYBACK_INTERVAL_MS:
            raise ValueError(
                f"playback range must be at least {MIN_PLAYBACK_INTERVAL_MS} ms"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def custom(self) -> bool:
        return self.start_ms > 0 or self.end_ms < self.source_duration_ms


@dataclass(frozen=True, slots=True)
class MediaTrim:
    """Lossless persisted trim offsets using JW Library's 100 ns ticks.

    ``end_trim_ticks`` is the amount removed from the end of the source, not an
    absolute timestamp. ``base_duration_ticks`` records the duration against
    which the edit was authored; runtime resolution still uses the active
    source duration supplied to :meth:`resolve`.
    """

    start_trim_ticks: int = 0
    end_trim_ticks: int = 0
    base_duration_ticks: int = 0

    def __post_init__(self) -> None:
        start_ticks = _validated_nonnegative_int(
            self.start_trim_ticks,
            field_name="start_trim_ticks",
        )
        end_ticks = _validated_nonnegative_int(
            self.end_trim_ticks,
            field_name="end_trim_ticks",
        )
        base_ticks = _validated_nonnegative_int(
            self.base_duration_ticks,
            field_name="base_duration_ticks",
        )
        if base_ticks and base_ticks - start_ticks - end_ticks < MIN_PLAYBACK_INTERVAL_TICKS:
            raise ValueError(
                f"trimmed playback interval must be at least {MIN_PLAYBACK_INTERVAL_MS} ms"
            )

    @property
    def custom(self) -> bool:
        return self.start_trim_ticks > 0 or self.end_trim_ticks > 0

    @classmethod
    def from_millisecond_bounds(
        cls,
        start_ms: float,
        end_ms: float,
        duration_ms: float,
    ) -> MediaTrim:
        """Create persisted JW offsets from absolute editor bounds."""
        values = (start_ms, end_ms, duration_ms)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise TypeError("millisecond bounds must be finite numbers")
        if duration_ms <= 0:
            raise ValueError("duration_ms must be greater than zero")
        if start_ms < 0 or end_ms > duration_ms or end_ms <= start_ms:
            raise ValueError("millisecond bounds must fit inside the source duration")
        return cls(
            start_trim_ticks=round(start_ms * TICKS_PER_MILLISECOND),
            end_trim_ticks=round((duration_ms - end_ms) * TICKS_PER_MILLISECOND),
            base_duration_ticks=round(duration_ms * TICKS_PER_MILLISECOND),
        )

    def resolve(self, actual_duration_ms: int) -> ResolvedPlaybackRange:
        """Resolve exact trim ticks into safe bounds for a millisecond player."""
        duration_ms = _validated_nonnegative_int(
            actual_duration_ms,
            field_name="actual_duration_ms",
        )
        if duration_ms == 0:
            raise ValueError("actual_duration_ms must be greater than zero")

        start_ms = _ticks_to_start_ms(self.start_trim_ticks)
        actual_duration_ticks = duration_ms * TICKS_PER_MILLISECOND
        end_ticks = actual_duration_ticks - self.end_trim_ticks
        end_ms = end_ticks // TICKS_PER_MILLISECOND
        return ResolvedPlaybackRange(
            start_ms=start_ms,
            end_ms=end_ms,
            source_duration_ms=duration_ms,
        )


class PlaybackCachePolicy(StrEnum):
    PROFILE_DEFAULT = "profile_default"
    PERSISTENT = "persistent"
    TEMPORARY = "temporary"


@dataclass(frozen=True, slots=True)
class MediaPlaybackRequest:
    """Complete immutable input for starting one media playback session."""

    source: str
    trim: MediaTrim | None = None
    autoplay: bool = True
    cache_policy: PlaybackCachePolicy = PlaybackCachePolicy.PROFILE_DEFAULT
    occurrence_id: str = ""
    occurrence_container_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.trim is not None and not isinstance(self.trim, MediaTrim):
            raise TypeError("trim must be a MediaTrim or None")
        if not isinstance(self.autoplay, bool):
            raise TypeError("autoplay must be a boolean")
        if not isinstance(self.cache_policy, PlaybackCachePolicy):
            raise TypeError("cache_policy must be a PlaybackCachePolicy")
        if not isinstance(self.occurrence_id, str):
            raise TypeError("occurrence_id must be a string")
        if not isinstance(self.occurrence_container_id, str):
            raise TypeError("occurrence_container_id must be a string")
