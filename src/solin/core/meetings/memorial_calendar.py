"""
memorial_calendar.py - Solin
====================================
Pure Memorial calendar rules.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)


def memorial_date_for_year(year: int) -> date | None:
    """Calculate the JW Memorial date for a modern Gregorian year."""

    try:
        import ephem
    except ImportError:
        log.error("ephem is not installed - cannot calculate the Memorial date")
        return None

    lag_min = 49.0
    lag_max = 150.0
    age_min = 22.0

    jerusalem = ephem.Observer()
    jerusalem.lat = "31.7683"
    jerusalem.lon = "35.2137"
    jerusalem.elevation = 754

    try:
        equinox = ephem.next_vernal_equinox(f"{year}/01/01")
        luna_prev = ephem.previous_new_moon(equinox)
        luna_next = ephem.next_new_moon(equinox)

        dist_prev = abs(equinox.datetime() - luna_prev.datetime())
        dist_next = abs(equinox.datetime() - luna_next.datetime())
        new_moon = luna_prev.datetime() if dist_prev < dist_next else luna_next.datetime()

        test_day = new_moon
        for _ in range(35):
            jerusalem.date = test_day
            sunset = jerusalem.next_setting(ephem.Sun())

            jerusalem.date = sunset
            moonset = jerusalem.next_setting(ephem.Moon())

            lag = (moonset.datetime() - sunset.datetime()).total_seconds() / 60.0
            age = (sunset.datetime() - new_moon).total_seconds() / 3600.0

            if lag_min <= lag <= lag_max and age >= age_min:
                nisan1 = sunset.datetime()
                memorial = nisan1 + timedelta(days=13)
                if memorial < datetime(year, 3, 22):
                    test_day = sunset.datetime() + timedelta(hours=20)
                    continue
                return memorial.date()

            test_day = sunset.datetime() + timedelta(hours=20)
    except Exception:  # noqa: BLE001 - ephem exposes implementation-specific exceptions
        log.exception("Failed to calculate Memorial date %d", year)

    return None


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


__all__ = ["memorial_date_for_year", "monday_of"]
