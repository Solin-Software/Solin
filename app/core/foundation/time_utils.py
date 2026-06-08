"""
time_utils.py — Solin
=====================
Small, dependency-light helpers for wall-clock countdown maths.
"""

from __future__ import annotations

from math import ceil

from PySide6.QtCore import QDateTime


def ceil_remaining_seconds(target: QDateTime, now: QDateTime | None = None) -> int:
    """Whole seconds remaining until ``target``, rounded *up* from milliseconds.

    Using the millisecond delta with a ceiling (instead of ``QDateTime.secsTo``,
    which truncates toward zero) keeps a countdown exact and smooth:

      • A target set to "now + 15 min" reads as ``900`` ("15:00") from the very
        first frame, rather than ``899`` ("14:59") because a few milliseconds
        already elapsed.
      • Each integer second is shown for its full second, so a fast (sub-second)
        refresh never displays the same value twice or skips one.

    Returns ``0`` once the target has passed (callers treat ``<= 0`` as expired).
    """
    effective_now = now if now is not None else QDateTime.currentDateTime()
    ms = effective_now.msecsTo(target)
    if ms <= 0:
        return 0
    return ceil(ms / 1000)
