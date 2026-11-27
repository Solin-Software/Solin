"""Track sustained silence in a single libobs playback source."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from math import isnan
from typing import Any

log = logging.getLogger(__name__)

_SILENCE_HOLD_FLAG = "SOLIN_ZOOM_PLAYBACK_SILENCE_MS"
_DEFAULT_SILENCE_HOLD_MS = 800
_QUIET_PEAK_DBFS = -60.0
_AUDIBLE_PEAK_DBFS = -54.0


def playback_silence_hold_ms() -> int:
    """Load the silence hold in milliseconds. Zero disables measurement."""
    value = os.environ.get(_SILENCE_HOLD_FLAG)
    if value is None:
        return _DEFAULT_SILENCE_HOLD_MS
    try:
        duration = int(value)
    except ValueError:
        duration = -1
    if duration >= 0:
        return duration
    log.warning("Invalid %s value %r; using %d ms",
                _SILENCE_HOLD_FLAG, value, _DEFAULT_SILENCE_HOLD_MS)
    return _DEFAULT_SILENCE_HOLD_MS


class MediaAudioActivity:
    """Combine source peaks with progress of the real playback timeline.

    Advancing video can confirm silence when the file has no audio track or
    the track ends early. A decoder waiting for data cannot confirm silence.
    """

    def __init__(
        self, hold_ms: int, *, monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._hold_ns = hold_ms * 1_000_000
        self._clock = monotonic_ns
        self._lock = threading.Lock()
        self._meter: Any | None = None
        self._audible = False
        self._previous_position_ms: int | None = None
        self._quiet_start_ms = 0
        self._quiet_started_ns = 0
        self._silent = False

    def attach(self, source: Any, create_meter: Callable[[], Any]) -> None:
        meter = self._meter = create_meter()
        meter.add_callback(self._observe_levels)
        meter.attach(source)

    def close(self) -> None:
        if self._meter is not None:
            self._meter.release()
            self._meter = None

    def _observe_levels(
        self, _magnitude: list[float], _peak: list[float], input_peak: list[float],
    ) -> None:
        # The input peak belongs only to this source and ignores the OBS
        # program mute used for monitor-only routing. The app gates the result
        # with its own playback volume and audio suppression.
        self.observe_peak(max(input_peak))

    def observe_peak(self, peak_dbfs: float) -> None:
        """Audio callback. Keep only the last audible peak, with hysteresis."""
        with self._lock:
            threshold = _QUIET_PEAK_DBFS if self._audible else _AUDIBLE_PEAK_DBFS
            self._audible = peak_dbfs > threshold or isnan(peak_dbfs)
            if self._audible:
                self._quiet_started_ns = self._clock()
                self._quiet_start_ms = self._previous_position_ms or 0
                self._silent = False

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        self._audible = False
        self._previous_position_ms = None
        self._silent = False

    def sample(self, position_ms: int, *, playing: bool, speed_percent: int) -> bool:
        """Return whether enough silence has passed while the transport advanced."""
        with self._lock:
            previous = self._previous_position_ms
            if not playing or (previous is not None and position_ms < previous):
                self._reset_locked()
                return False

            now = self._clock()
            if previous is None:
                self._quiet_start_ms = position_ms
                self._quiet_started_ns = now
            elif position_ms > previous:
                observed_ns = now - self._quiet_started_ns
                advanced_ms = position_ms - self._quiet_start_ms
                advanced_ns = advanced_ms * 1_000_000 * 100 // speed_percent
                if min(observed_ns, advanced_ns) >= self._hold_ns:
                    self._silent = True
                    self._audible = False
            self._previous_position_ms = position_ms
            return self._silent
