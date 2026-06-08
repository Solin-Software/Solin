"""
app.core.timer — Solin
=======================
Pure-domain timer/clock subsystem for the advanced meeting timer.

This package is deliberately free of any QtWidgets/QML dependency (only
QtCore for the engine's QObject/QTimer). The domain is the single source of
truth: the QML UI and a *future* local-network adapter both observe the very
same engine signals and serialize the same ``TimerSnapshot`` — so exposing the
timer over HTTP/WebSocket later needs no rewrite of this layer.

Public surface:
    models           — serializable dataclasses + enums
    schedule_factory — builds default meeting schedules and redistributes time
    engine           — TimerEngine: authoritative live clock/stopwatch
    store            — TimerStore: per-profile persistence
"""

from .models import (
    ANALOG_CLOCK_STYLE_OPTIONS,
    CLOCK_MODE_OPTIONS,
    PART_TIMER_DISPLAY_OPTIONS,
    AnalogClockStyle,
    ClockConfig,
    MeetingPart,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    PartTimerDisplay,
    Section,
    TimerSnapshot,
)
from .engine import TimerEngine
from .schedule_factory import (
    build_default_schedule,
    normalize_schedule,
    redistribute_section,
    schedule_has_timing_data,
    set_section_part_count,
)
from .store import TimerStore

__all__ = [
    "ANALOG_CLOCK_STYLE_OPTIONS",
    "CLOCK_MODE_OPTIONS",
    "PART_TIMER_DISPLAY_OPTIONS",
    "AnalogClockStyle",
    "ClockConfig",
    "MeetingPart",
    "MeetingSchedule",
    "MeetingType",
    "PartKind",
    "PartState",
    "PartTimerDisplay",
    "Section",
    "TimerSnapshot",
    "TimerEngine",
    "TimerStore",
    "build_default_schedule",
    "normalize_schedule",
    "redistribute_section",
    "schedule_has_timing_data",
    "set_section_part_count",
]
