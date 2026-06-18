"""
folder_matcher.py — Solin
==================================
Classifies linked-folder subfolder names into meeting targets.

A subfolder whose name matches the pattern  ``YYYY-MM-DD MW|WE``  is
recognised as a "meeting folder".  The date identifies the JW meeting
week (Monday → Sunday), and the tag identifies the meeting type.

Examples
--------
- ``2026-05-26 MW`` → midweek meeting for the week of 25–31 May 2026
- ``2026-05-31 WE`` → weekend meeting for the same week
- ``2026-05-28 mw`` → case-insensitive — same week, midweek
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(MW|WE)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MeetingFolderMatch:
    """Result of matching a subfolder name to a meeting target."""

    monday: date      # Start of the JW meeting week (always a Monday)
    meeting_tag: str   # ``"MW"`` or ``"WE"`` (normalised upper-case)


def match_meeting_folder(name: str) -> Optional[MeetingFolderMatch]:
    """Return a :class:`MeetingFolderMatch` if *name* follows the convention,
    else ``None``.

    The date component can be *any* day within the week — the returned
    ``monday`` is always the ISO Monday of that week.
    """
    m = _PATTERN.match(name.strip())
    if not m:
        return None
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        return None
    # Monday of that week (ISO weekday: Mon=0 … Sun=6)
    monday = d - timedelta(days=d.weekday())
    tag = m.group(2).upper()   # "MW" or "WE"
    return MeetingFolderMatch(monday=monday, meeting_tag=tag)


def is_meeting_folder(name: str) -> bool:
    """Quick predicate — does this subfolder name match the convention?"""
    return match_meeting_folder(name) is not None
