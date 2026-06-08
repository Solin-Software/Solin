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

from .models import ClockConfig, ClockMode, PartTimerDisplay, TimerSnapshot


def _fmt_clock(epoch: float, config: ClockConfig) -> tuple[str, str, str]:
    """Return (primary_text, seconds_text, ampm_text) for the wall clock."""
    lt = time.localtime(epoch)
    if config.hour_format_24h:
        text = time.strftime("%H:%M", lt)
        seconds = time.strftime("%S", lt) if config.show_seconds else ""
        return text, seconds, ""
    text = time.strftime("%I:%M", lt).lstrip("0")
    seconds = time.strftime("%S", lt) if config.show_seconds else ""
    ampm = time.strftime("%p", lt) if config.show_ampm else ""
    return text, seconds, ampm


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


def _blank_face(config: ClockConfig) -> dict:
    return {
        "mode": config.mode.value,
        "analog_style": config.analog_style.value,
        "primary_text": "",
        "seconds_text": "",
        "secondary_text": "",
        "overrun": False,
        "hour_angle": 0.0,
        "minute_angle": 0.0,
        "second_angle": 0.0,
    }


def _clock_face(epoch: float, config: ClockConfig) -> dict:
    face = _blank_face(config)
    if config.mode in (ClockMode.ANALOG, ClockMode.ANALOG_DIGITAL):
        # The timer engine ticks faster than once per second so countdowns stay
        # smooth. Analog clocks should not inherit that cadence: the second hand
        # advances in whole-second steps, while minute/hour hands follow that
        # same stable wall-clock instant.
        whole_epoch = math.floor(epoch)
        lt = time.localtime(whole_epoch)
        second = lt.tm_sec
        minute = lt.tm_min + second / 60.0
        hour = (lt.tm_hour % 12) + minute / 60.0
        face["second_angle"] = second * 6.0
        face["minute_angle"] = minute * 6.0
        face["hour_angle"] = hour * 30.0
        # Analog projection is face-only unless the combined face is selected.
        if config.mode is ClockMode.ANALOG_DIGITAL:
            text, seconds, ampm = _fmt_clock(epoch, config)
            face["primary_text"] = text
            face["seconds_text"] = seconds
            face["secondary_text"] = ampm
        return face

    text, seconds, ampm = _fmt_clock(epoch, config)
    face["primary_text"] = text
    face["seconds_text"] = seconds
    face["secondary_text"] = ampm
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
        "hour_angle": 0.0,
        "minute_angle": 0.0,
        "second_angle": 0.0,
    }


def _select_active_display(config: ClockConfig) -> str:
    if config.part_timer_display is PartTimerDisplay.CLOCK:
        return "clock"
    if config.part_timer_display is PartTimerDisplay.CLOCK_TIMER:
        return "clock_timer"
    return "timer"


def build_render_model(snapshot: TimerSnapshot, config: ClockConfig) -> dict:
    """Flat dict consumed by ClockFace.qml.

    Keys:
        active          — bool: a meeting part is running/frozen.
        display_mode    — "clock" | "timer" | "clock_timer".
        clock           — render payload for the configured wall clock.
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
    clock_face = _clock_face(snapshot.wall_clock_epoch, config)
    timer_face = _timer_face(snapshot) if snapshot.active else _blank_face(config)
    display_mode = "clock"
    selected_face = clock_face
    if snapshot.active:
        display_mode = _select_active_display(config)
        if display_mode == "timer":
            selected_face = timer_face

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
