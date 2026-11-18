"""Meeting links: short codes, passwords and the schedule they belong to (Qt-free).

A **link** is a 6-character code plus a password. Attendees open
``<origin>/<code>``; the stage page is ``/<code>/s`` and the moderator page
``/<code>/m``. Every page asks for the same password.

Two modes (``LinkMode``):

* **same** — one permanent link for every meeting, until "New link";
* **per_meeting** — a link for each scheduled meeting, prepared
  :data:`HORIZON` ahead from the congregation's midweek and weekend meetings.

Each scheduled meeting has a **window**: it opens :data:`WINDOW_BEFORE` before the
start and closes :data:`WINDOW_AFTER` after the worst-case end
(:data:`MEETING_LENGTH`). The official attendance count is taken at
:data:`COUNT_FRACTION` of the meeting's length.

Codes use lowercase consonants and the digits 2–9: no ``0 o 1 l i``, which are easy
to confuse, and no vowels, so a code never spells a word. A retired code is never
handed out again by this congregation. (When the hub issues codes it guarantees
uniqueness across congregations; until then Solin draws them itself.)
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any

from solin.core.meetings.schedule import MeetingSchedule

CODE_ALPHABET = "23456789bcdfghjkmnpqrstvwxyz"
CODE_LENGTH = 6
PASSWORD_LENGTH = 6
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 32

WINDOW_BEFORE = timedelta(minutes=30)
MEETING_LENGTH = timedelta(hours=2)
WINDOW_AFTER = timedelta(minutes=30)
COUNT_FRACTION = 0.75
HORIZON = timedelta(weeks=4)
LOW_CODES = timedelta(weeks=2)
# A retired code is remembered (and never drawn again) for this long.
RETIRED_FOR = timedelta(days=400)

UNSCHEDULED = "unscheduled"
EXTRA = "extra"

_CODE = re.compile(f"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")
_SEPARATORS = re.compile(r"[\s\-_.]+")


class LinkMode(StrEnum):
    SAME = "same"
    PER_MEETING = "per_meeting"


def normalize_code(text: str) -> str | None:
    """The code someone typed or read aloud, or None: capitals, spaces and dashes are fine."""

    code = _SEPARATORS.sub("", str(text or "")).lower()
    return code if _CODE.match(code) else None


def new_code(taken: Iterable[str] = ()) -> str:
    used = set(taken)
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in used:
            return code


def new_password() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(PASSWORD_LENGTH))


def clean_password(text: str) -> str | None:
    """An operator-chosen password, or None when it is too short, too long or has spaces."""

    password = str(text or "").strip()
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH or any(c.isspace() for c in password):
        return None
    return password


@dataclass(frozen=True, slots=True)
class Link:
    code: str
    password: str


@dataclass(frozen=True, slots=True)
class ScheduledMeeting:
    """One meeting with its own link (per-meeting mode) or a time on the permanent link."""

    id: str                 # "<kind>:<original date>" for weekly meetings, random for one-offs
    kind: str               # midweek / weekend / extra / unscheduled
    starts_at: datetime     # aware
    code: str = ""
    password: str = ""
    cancelled: bool = False
    moved: bool = False     # the operator changed the time: regeneration keeps it

    @property
    def opens_at(self) -> datetime:
        return self.starts_at - WINDOW_BEFORE

    @property
    def closes_at(self) -> datetime:
        return self.starts_at + MEETING_LENGTH + WINDOW_AFTER

    @property
    def count_at(self) -> datetime:
        return self.starts_at + MEETING_LENGTH * COUNT_FRACTION

    @property
    def scheduled(self) -> bool:
        return self.kind != UNSCHEDULED

    def in_window(self, now: datetime) -> bool:
        return not self.cancelled and self.opens_at <= now < self.closes_at

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "startsAt": self.starts_at.isoformat(), "code": self.code,
                "password": self.password, "cancelled": self.cancelled, "moved": self.moved}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledMeeting | None:
        try:
            starts_at = datetime.fromisoformat(str(data["startsAt"]))
        except (KeyError, ValueError):
            return None
        if starts_at.tzinfo is None:
            return None
        code = normalize_code(str(data.get("code") or "")) or ""
        return cls(id=str(data.get("id") or ""), kind=str(data.get("kind") or EXTRA), starts_at=starts_at,
                   code=code, password=str(data.get("password") or ""), cancelled=bool(data.get("cancelled")),
                   moved=bool(data.get("moved")))


@dataclass(frozen=True, slots=True)
class LinksState:
    """Everything the profile stores about its links (JSON in the profile settings)."""

    permanent: Link | None = None
    meetings: tuple[ScheduledMeeting, ...] = ()
    retired: dict[str, str] = field(default_factory=dict)   # code -> ISO time it was retired

    # ── persistence ───────────────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 2,
            "permanent": {"code": self.permanent.code, "password": self.permanent.password} if self.permanent else None,
            "meetings": [m.as_dict() for m in self.meetings],
            "retired": dict(self.retired),
        }

    @classmethod
    def from_json(cls, data: Any) -> LinksState:
        if not isinstance(data, dict) or data.get("version") != 2:
            return cls()
        permanent = None
        raw = data.get("permanent")
        if isinstance(raw, dict):
            code = normalize_code(str(raw.get("code") or ""))
            password = clean_password(str(raw.get("password") or ""))
            if code and password:
                permanent = Link(code, password)
        meetings = tuple(m for m in (ScheduledMeeting.from_dict(item) for item in data.get("meetings") or []
                                     if isinstance(item, dict)) if m is not None)
        retired = {str(k): str(v) for k, v in (data.get("retired") or {}).items()} \
            if isinstance(data.get("retired"), dict) else {}
        return cls(permanent=permanent, meetings=meetings, retired=retired)

    # ── queries ───────────────────────────────────────────────────────────

    def codes_in_use(self) -> set[str]:
        codes = {m.code for m in self.meetings if m.code}
        if self.permanent is not None:
            codes.add(self.permanent.code)
        return codes | set(self.retired)

    def upcoming(self, now: datetime) -> list[ScheduledMeeting]:
        """Meetings whose window has not closed yet, in start order (cancelled included)."""

        return sorted((m for m in self.meetings if m.closes_at > now), key=lambda m: m.starts_at)

    def in_window(self, now: datetime) -> ScheduledMeeting | None:
        """The meeting whose window is open now (the earliest, if two overlap)."""

        open_now = [m for m in self.meetings if m.in_window(now)]
        return min(open_now, key=lambda m: m.starts_at) if open_now else None

    def find(self, meeting_id: str) -> ScheduledMeeting | None:
        return next((m for m in self.meetings if m.id == meeting_id), None)

    def low_on_codes(self, schedule: MeetingSchedule, now: datetime) -> bool:
        """True when weekly meetings exist but fewer than two weeks of links are prepared."""

        if not schedule.has_configured_slot:
            return False
        last = max((m.starts_at for m in self.meetings if m.code and not m.cancelled), default=None)
        return last is None or last < now + LOW_CODES

    # ── changes (each returns a new state) ────────────────────────────────

    def with_permanent(self) -> LinksState:
        """The permanent link, created on first use."""

        if self.permanent is not None:
            return self
        return replace(self, permanent=Link(new_code(self.codes_in_use()), new_password()))

    def retire(self, codes: Iterable[str], now: datetime) -> dict[str, str]:
        retired = dict(self.retired)
        for code in codes:
            if code:
                retired[code] = now.isoformat()
        return retired

    def new_permanent(self, now: datetime) -> LinksState:
        old = [self.permanent.code] if self.permanent else []
        retired = self.retire(old, now)
        taken = self.codes_in_use() | set(retired)
        return replace(self, permanent=Link(new_code(taken), new_password()), retired=retired)

    def set_permanent_password(self, password: str) -> LinksState:
        state = self.with_permanent()
        assert state.permanent is not None
        return replace(state, permanent=Link(state.permanent.code, password))

    def top_up(self, schedule: MeetingSchedule, now: datetime, *, with_links: bool) -> LinksState:
        """Weekly meetings for the next four weeks; meetings whose window closed are dropped.

        ``with_links`` gives every meeting its own code and password (per-meeting mode).
        In same-link mode the meetings only carry their times (countdown, automatic
        start, official count).
        """

        keep = [m for m in self.meetings if m.closes_at > now]
        dropped = [m.code for m in self.meetings if m.closes_at <= now]
        retired = self.retire(dropped, now)
        # Forget retired codes after a long time (they are safe to draw again by then).
        retired = {code: when for code, when in retired.items() if _parse(when) > now - RETIRED_FOR}
        known = {m.id for m in keep}
        taken = {m.code for m in keep if m.code} | set(retired)
        if self.permanent is not None:
            taken.add(self.permanent.code)
        for occurrence in weekly_occurrences(schedule, now, now + HORIZON):
            if occurrence.id in known:
                continue
            keep.append(occurrence)
            known.add(occurrence.id)
        meetings: list[ScheduledMeeting] = []
        for meeting in sorted(keep, key=lambda m: m.starts_at):
            if with_links and not meeting.code:
                code = new_code(taken)
                taken.add(code)
                meeting = replace(meeting, code=code, password=new_password())
            meetings.append(meeting)
        return replace(self, meetings=tuple(meetings), retired=retired)

    def renew(self, meeting_id: str, now: datetime) -> LinksState:
        meeting = self.find(meeting_id)
        if meeting is None:
            return self
        retired = self.retire([meeting.code], now)
        code = new_code(self.codes_in_use() | set(retired))
        renewed = replace(meeting, code=code, password=new_password())
        return replace(self, meetings=tuple(renewed if m.id == meeting_id else m for m in self.meetings),
                       retired=retired)

    def renew_upcoming(self, now: datetime, *, skip: str = "") -> LinksState:
        state = self
        for meeting in self.upcoming(now):
            if meeting.id != skip and meeting.code:
                state = state.renew(meeting.id, now)
        return state

    def set_meeting_password(self, meeting_id: str, password: str) -> LinksState:
        return self._replace_meeting(meeting_id, password=password)

    def move(self, meeting_id: str, starts_at: datetime) -> LinksState:
        return self._replace_meeting(meeting_id, starts_at=starts_at, moved=True)

    def set_cancelled(self, meeting_id: str, cancelled: bool) -> LinksState:
        return self._replace_meeting(meeting_id, cancelled=cancelled)

    def add_meeting(self, starts_at: datetime, *, with_link: bool, kind: str = EXTRA) -> tuple[LinksState, str]:
        meeting_id = f"{kind}:{secrets.token_hex(4)}"
        code = new_code(self.codes_in_use()) if with_link else ""
        meeting = ScheduledMeeting(meeting_id, kind, starts_at, code=code,
                                   password=new_password() if with_link else "", moved=True)
        meetings = sorted([*self.meetings, meeting], key=lambda m: m.starts_at)
        return replace(self, meetings=tuple(meetings)), meeting_id

    def remove_meeting(self, meeting_id: str, now: datetime) -> LinksState:
        meeting = self.find(meeting_id)
        if meeting is None:
            return self
        return replace(self, meetings=tuple(m for m in self.meetings if m.id != meeting_id),
                       retired=self.retire([meeting.code], now))

    def _replace_meeting(self, meeting_id: str, **changes: Any) -> LinksState:
        return replace(self, meetings=tuple(replace(m, **changes) if m.id == meeting_id else m
                                            for m in self.meetings))


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def weekly_occurrences(schedule: MeetingSchedule, start: datetime, end: datetime) -> list[ScheduledMeeting]:
    """The configured weekly meetings whose window has not closed by ``start``, up to ``end``."""

    found: list[ScheduledMeeting] = []
    for slot in schedule.slots:
        if not slot.is_configured or slot.start_minutes is None:
            continue
        clock = time(slot.start_minutes // 60, slot.start_minutes % 60)
        day = start.date() - timedelta(days=1)
        while True:
            if day.weekday() == slot.weekday:
                starts_at = datetime.combine(day, clock, tzinfo=start.tzinfo)
                if starts_at > end:
                    break
                meeting = ScheduledMeeting(f"{slot.kind}:{day.isoformat()}", slot.kind, starts_at)
                if meeting.closes_at > start:
                    found.append(meeting)
            day += timedelta(days=1)
            if datetime.combine(day, time(0), tzinfo=start.tzinfo) > end:
                break
    return sorted(found, key=lambda m: m.starts_at)
