"""
render.py — Solin timer domain
==============================
Turns a (``TimerSnapshot``, ``ClockConfig``) pair into a flat *render model*
dict that the QML clock face renders declaratively.

All formatting and clock-hand geometry live here (pure Python) so the QML stays
dumb, the behaviour is unit-testable, and a future web/network client can reuse
the exact same model.
"""

from __future__ import annotations

import math
import time

from .models import ClockConfig, ClockMode, PartState, PartTimerDisplay, TimerSnapshot


_SECONDS_PER_ANALOG_LAP = 3600.0


def format_fixed_countdown(remaining_seconds: int, total_seconds: int) -> str:
    """Format a media countdown without changing its field shape while it runs.

    A countdown that starts below one hour always remains ``MM:SS``. A longer
    countdown always keeps its hour field, including leading zeroes after it
    drops below one hour.
    """
    remaining = max(0, int(remaining_seconds))
    total = max(1, int(total_seconds))
    hours, remainder = divmod(remaining, 3600)
    minutes, seconds = divmod(remainder, 60)

    if total < 3600:
        return f"{minutes:02d}:{seconds:02d}"

    initial_hours = total // 3600
    hour_width = max(2, len(str(initial_hours)))
    return f"{hours:0{hour_width}d}:{minutes:02d}:{seconds:02d}"


def _digital_clock_fields(lt: time.struct_time, config: ClockConfig) -> dict[str, str]:
    """The digital form of a wall-clock instant, using the configured format."""
    if config.hour_format_24h:
        text = time.strftime("%H:%M", lt)
    else:
        text = time.strftime("%I:%M", lt).lstrip("0")
    return {
        "primary_text": text,
        "seconds_text": time.strftime("%S", lt) if config.show_seconds else "",
        "secondary_text": (
            time.strftime("%p", lt) if not config.hour_format_24h and config.show_ampm else ""
        ),
    }


def _fmt_duration(seconds: float) -> tuple[str, str]:
    """Return (primary_text, seconds_text) for a timer duration.

    Durations under one hour stay as a conventional MM:SS readout. Once the
    hour field appears, seconds move to the small secondary readout so the
    large timer stays compact.
    """
    sign = "-" if seconds < 0 else ""
    total = int(round(abs(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{sign}{h}:{m:02d}", f"{s:02d}"
    return f"{sign}{m:02d}:{s:02d}", ""


def _minute_angle_for_epoch(epoch: float) -> float:
    whole_epoch = math.floor(epoch)
    lt = time.localtime(whole_epoch)
    return (lt.tm_min * 6.0) + (lt.tm_sec * 0.1)


def _blank_face(config: ClockConfig) -> dict:
    return {
        "mode": config.mode.value,
        "analog_style": config.analog_style.value,
        "primary_text": "",
        "seconds_text": "",
        "secondary_text": "",
        "overrun": False,
        "duration_sector": {"visible": False},
        "hour_angle": 0.0,
        "minute_angle": 0.0,
        "second_angle": 0.0,
    }


def _clock_face(epoch: float, config: ClockConfig, *, compact_readout: bool) -> dict:
    face = _blank_face(config)
    lt = time.localtime(math.floor(epoch))
    if config.mode in (ClockMode.ANALOG, ClockMode.ANALOG_DIGITAL):
        # The timer engine ticks faster than once per second so countdowns stay
        # smooth. Analog clocks should not inherit that cadence: the second hand
        # advances in whole-second steps, while minute/hour hands follow that
        # same stable wall-clock instant.
        second = lt.tm_sec
        minute = lt.tm_min + second / 60.0
        hour = (lt.tm_hour % 12) + minute / 60.0
        face["second_angle"] = second * 6.0
        face["minute_angle"] = minute * 6.0
        face["hour_angle"] = hour * 30.0
        if config.mode is ClockMode.ANALOG:
            if compact_readout:
                face["compact_readout"] = _digital_clock_fields(lt, config)
            return face

    face.update(_digital_clock_fields(lt, config))
    return face


def _timer_face(snapshot: TimerSnapshot) -> dict:
    primary, seconds = _fmt_duration(snapshot.display_seconds())
    return {
        "mode": ClockMode.DIGITAL.value,
        "analog_style": "",
        "primary_text": primary,
        "seconds_text": seconds,
        "secondary_text": "",
        "overrun": bool(snapshot.overrun and snapshot.active),
        "duration_sector": {"visible": False},
        "hour_angle": 0.0,
        "minute_angle": 0.0,
        "second_angle": 0.0,
    }


def _duration_sector(snapshot: TimerSnapshot) -> dict:
    if (
        not snapshot.active
        or snapshot.state is PartState.STOPPED
        or snapshot.planned_seconds <= 0
    ):
        return {"visible": False}

    remaining = float(snapshot.remaining_seconds)
    current_epoch = snapshot.wall_clock_epoch
    overrun_seconds = max(0.0, -remaining)

    end_epoch = current_epoch + remaining
    show_remaining = 0.0 < remaining < _SECONDS_PER_ANALOG_LAP
    show_overrun = snapshot.overrun and overrun_seconds < _SECONDS_PER_ANALOG_LAP

    return {
        "visible": bool(show_remaining or show_overrun),
        "show_remaining": bool(show_remaining),
        "show_overrun": bool(show_overrun),
        "end_angle": _minute_angle_for_epoch(end_epoch),
        "current_angle": _minute_angle_for_epoch(current_epoch),
    }


def _select_active_display(config: ClockConfig) -> str:
    if config.part_timer_display is PartTimerDisplay.CLOCK:
        return "clock"
    if config.part_timer_display is PartTimerDisplay.CLOCK_TIMER:
        return "clock_timer"
    return "timer"


def build_render_model(
    snapshot: TimerSnapshot, config: ClockConfig, *, compact_readout: bool = False
) -> dict:
    """Flat dict consumed by ClockFace.qml.

    Keys:
        active          — bool: a meeting part is running/frozen.
        display_mode    — "clock" | "timer" | "clock_timer".
        clock           — render payload for the configured wall clock, with
                          optional digital compact_readout for the analog face.
        timer           — render payload for the active part timer.
        mode            — "analog" | "analog_digital" | "digital" (governs
                          the currently selected single-face payload).
        analog_style    — selected analog face id for analog idle mode.
        primary_text    — the large text (countdown when active, else digital
                          clock without the trailing seconds; empty in
                          analog-idle, which draws hands).
        seconds_text    — small trailing seconds, without a leading colon.
        secondary_text  — AM/PM suffix for the idle digital wall clock.
        overrun         — bool: countdown went negative (paint red).
        show_seconds    — bool.
        text_scale_pct  — int, 10..100 percent of the largest fitting content.
        hour_angle / minute_angle / second_angle — degrees, for analog idle.
    """
    clock_face = _clock_face(snapshot.wall_clock_epoch, config, compact_readout=compact_readout)
    timer_face = _timer_face(snapshot) if snapshot.active else _blank_face(config)
    display_mode = "clock"
    selected_face = clock_face
    if snapshot.active:
        display_mode = _select_active_display(config)
        if display_mode == "timer":
            selected_face = timer_face
        elif config.mode in (ClockMode.ANALOG, ClockMode.ANALOG_DIGITAL):
            clock_face["duration_sector"] = _duration_sector(snapshot)

    model: dict = {
        "active": snapshot.active,
        "display_mode": display_mode,
        "clock": clock_face,
        "timer": timer_face,
        "mode": selected_face["mode"],
        "analog_style": selected_face["analog_style"],
        "primary_text": selected_face["primary_text"],
        "seconds_text": selected_face["seconds_text"],
        "secondary_text": selected_face["secondary_text"],
        "overrun": selected_face["overrun"] if display_mode == "timer" else timer_face["overrun"],
        "show_seconds": config.show_seconds,
        "text_scale_pct": int(config.text_scale_pct),
        "hour_angle": selected_face["hour_angle"],
        "minute_angle": selected_face["minute_angle"],
        "second_angle": selected_face["second_angle"],
    }
    return model
