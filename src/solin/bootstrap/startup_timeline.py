from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from time import perf_counter_ns
from typing import TextIO


_TRACE_ENVIRONMENT_VARIABLE = "SOLIN_STARTUP_TRACE"
_BENCHMARK_EXIT_ENVIRONMENT_VARIABLE = "SOLIN_STARTUP_BENCHMARK_EXIT"


@dataclass(frozen=True, slots=True)
class StartupMark:
    name: str
    elapsed_ms: float


class StartupTimeline:
    """Process-local startup instrumentation without profile or user data."""

    def __init__(self, *, started_ns: int | None = None, enabled: bool | None = None) -> None:
        self._started_ns = perf_counter_ns() if started_ns is None else started_ns
        self._enabled = _trace_requested() if enabled is None else enabled
        self._marks: list[StartupMark] = []
        self._marked_names: set[str] = set()
        self._measurements: dict[str, tuple[float, ...]] = {}
        self._emitted = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def mark(self, name: str, *, observed_ns: int | None = None) -> StartupMark | None:
        if not self._enabled:
            return None
        now_ns = perf_counter_ns() if observed_ns is None else observed_ns
        mark = StartupMark(
            name=name,
            elapsed_ms=round((now_ns - self._started_ns) / 1_000_000, 3),
        )
        with self._lock:
            if name in self._marked_names:
                return None
            self._marked_names.add(name)
            self._marks.append(mark)
        return mark

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            marks = tuple(self._marks)
            measurements = dict(self._measurements)
        snapshot: dict[str, object] = {
            "schema": "solin.startup.v1",
            "phases": [
                {"name": mark.name, "elapsed_ms": mark.elapsed_ms} for mark in marks
            ],
        }
        if measurements:
            snapshot["measurements_ms"] = measurements
        return snapshot

    def record_measurements(self, name: str, values: tuple[float, ...]) -> None:
        if not self._enabled:
            return
        finite_values = tuple(round(float(value), 3) for value in values if value >= 0)
        with self._lock:
            self._measurements[name] = finite_values

    def emit_json(self, stream: TextIO | None = None) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            if self._emitted:
                return False
            self._emitted = True
        output = stream or sys.stdout
        print(json.dumps(self.snapshot(), separators=(",", ":")), file=output, flush=True)
        return True


def _trace_requested() -> bool:
    value = os.environ.get(_TRACE_ENVIRONMENT_VARIABLE, "")
    return value.strip().casefold() in {"1", "true", "yes", "json"}


_PROCESS_TIMELINE = StartupTimeline()


def startup_timeline() -> StartupTimeline:
    return _PROCESS_TIMELINE


def startup_benchmark_exit_requested() -> bool:
    value = os.environ.get(_BENCHMARK_EXIT_ENVIRONMENT_VARIABLE, "")
    return value.strip().casefold() in {"1", "true", "yes"}


__all__ = [
    "StartupMark",
    "StartupTimeline",
    "startup_benchmark_exit_requested",
    "startup_timeline",
]
