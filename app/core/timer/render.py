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

from .models import ClockConfig, ClockMode, TimerSnapshot


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


def build_render_model(snapshot: TimerSnapshot, config: ClockConfig) -> dict:
    """Flat dict consumed by ClockFace.qml.

    Keys:
        active          — bool: a meeting part is running/frozen.
        mode            — "analog" | "analog_digital" | "digital" (governs
                          the *idle* clock).
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
    model: dict = {
        "active": snapshot.active,
        "mode": config.mode.value,
        "analog_style": config.analog_style.value,
        "primary_text": "",
        "seconds_text": "",
        "secondary_text": "",
        "overrun": bool(snapshot.overrun and snapshot.active),
        "show_seconds": config.show_seconds,
        "text_scale_pct": int(config.text_scale_pct),
        "hour_angle": 0.0,
        "minute_angle": 0.0,
        "second_angle": 0.0,
    }

    if snapshot.active:
        # Active part: always a large digital countdown/up, regardless of the
        # idle clock mode. ``display_seconds`` already respects the direction.
        model["mode"] = ClockMode.DIGITAL.value
        primary, seconds = _fmt_duration(snapshot.display_seconds())
        model["primary_text"] = primary
        model["seconds_text"] = seconds
        return model

    # Idle: show the wall clock in the configured mode.
    epoch = snapshot.wall_clock_epoch
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
        model["second_angle"] = second * 6.0
        model["minute_angle"] = minute * 6.0
        model["hour_angle"] = hour * 30.0
        # Analog projection is face-only unless the combined face is selected.
        if config.mode is ClockMode.ANALOG_DIGITAL:
            text, seconds, ampm = _fmt_clock(epoch, config)
            model["primary_text"] = text
            model["seconds_text"] = seconds
            model["secondary_text"] = ampm
    else:
        text, seconds, ampm = _fmt_clock(epoch, config)
        model["primary_text"] = text
        model["seconds_text"] = seconds
        model["secondary_text"] = ampm
    return model
