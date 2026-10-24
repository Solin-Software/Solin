"""The meeting's rules, independent of Qt, the web server and Galene's wire format.

Attendees are identified by a *device*: a random id plus a secret the browser keeps in
``localStorage``. The core decides who waits in the lobby, who is admitted (and gets
short-lived Galene join tokens), whose microphone is open, the raised-hands queue and
attendance. It talks to the outside through :class:`CoreEffects`:

* ``notify(device, message)`` — push a message to that browser (lobby → admitted,
  hand lowered, renamed, removed, meeting ended);
* ``present`` / ``unpresent`` / ``kick`` — Galene moderation on a Galene client id;
* ``changed()`` — the operator-facing snapshot changed.

Meeting rules (JW guidance): attendees cannot unmute themselves while the microphones
are locked; the operator calls on one commenter at a time (microphone only); a raised
hand is lowered when called on; remote part-holders may present camera and microphone;
removed devices get no new tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .galene_client import GaleneUser
from .tokens import OBSERVE, PRESENT

MAX_NAME_LENGTH = 60
MAX_LOBBY = 500


class AttendeeState(StrEnum):
    LOBBY = "lobby"
    ADMITTED = "admitted"
    REMOVED = "removed"


class JoinStatus(StrEnum):
    LOBBY = "lobby"
    ADMITTED = "admitted"
    REFUSED = "refused"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class JoinDecision:
    status: JoinStatus
    token: str = ""
    reason: str = ""


class CoreEffects(Protocol):
    def notify(self, device_id: str, message: dict[str, Any]) -> None: ...
    def present(self, galene_id: str) -> None: ...
    def unpresent(self, galene_id: str) -> None: ...
    def kick(self, galene_id: str, reason: str) -> None: ...
    def changed(self) -> None: ...


TokenMinter = Callable[[str, tuple[str, ...]], str]  # (username, permissions) -> token


@dataclass(slots=True)
class Attendee:
    device_id: str
    name: str
    secret_hash: str
    state: AttendeeState = AttendeeState.LOBBY
    requested_at: float = 0.0
    admitted_at: float | None = None
    part_holder: bool = False
    galene_ids: set[str] = field(default_factory=set)
    hand_raised_at: float | None = None
    hand_value: Any = None      # the client's raisehand value currently counted as raised
    hand_lowered: Any = None    # a value the hall lowered: ignored until the client sends a new one
    live: bool = False  # the operator called on them (microphone open)

    @property
    def connected(self) -> bool:
        return bool(self.galene_ids)


def clean_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    return cleaned[:MAX_NAME_LENGTH]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class MeetingCore:
    def __init__(self, effects: CoreEffects, mint: TokenMinter, *, clock: Callable[[], float] = time.time) -> None:
        self._effects = effects
        self._mint = mint
        self._clock = clock
        self.attendees: dict[str, Attendee] = {}
        self._by_galene: dict[str, str] = {}
        self.mics_locked = True
        self.ended = False
        self.stage_key = secrets.token_urlsafe(18)

    # ── browser side ──────────────────────────────────────────────────────

    def _authenticate(self, device_id: str, secret: str) -> Attendee | None:
        attendee = self.attendees.get(device_id)
        if attendee is None or not hmac.compare_digest(attendee.secret_hash, _hash(secret)):
            return None
        return attendee

    def request_join(self, device_id: str, secret: str, name: str) -> JoinDecision:
        if self.ended:
            return JoinDecision(JoinStatus.ENDED)
        if not device_id or len(secret) < 16:
            return JoinDecision(JoinStatus.REFUSED, reason="invalid device")
        name = clean_name(name)
        if not name:
            return JoinDecision(JoinStatus.REFUSED, reason="name required")
        attendee = self.attendees.get(device_id)
        if attendee is not None:
            if not hmac.compare_digest(attendee.secret_hash, _hash(secret)):
                return JoinDecision(JoinStatus.REFUSED, reason="invalid device")
            if attendee.state is AttendeeState.REMOVED:
                return JoinDecision(JoinStatus.REFUSED, reason="removed")
            if attendee.state is AttendeeState.ADMITTED:
                # Same device coming back (reload, network change): readmit at once.
                return JoinDecision(JoinStatus.ADMITTED, token=self._token(attendee))
            attendee.name = name
            self._effects.changed()
            return JoinDecision(JoinStatus.LOBBY)
        if sum(1 for a in self.attendees.values() if a.state is AttendeeState.LOBBY) >= MAX_LOBBY:
            return JoinDecision(JoinStatus.REFUSED, reason="lobby full")
        self.attendees[device_id] = Attendee(
            device_id=device_id, name=name, secret_hash=_hash(secret), requested_at=self._clock(),
        )
        self._effects.changed()
        return JoinDecision(JoinStatus.LOBBY)

    def token_for(self, device_id: str, secret: str) -> JoinDecision:
        """A fresh join token for an admitted device (used on every reconnect)."""

        if self.ended:
            return JoinDecision(JoinStatus.ENDED)
        attendee = self._authenticate(device_id, secret)
        if attendee is None:
            return JoinDecision(JoinStatus.REFUSED, reason="unknown device")
        if attendee.state is AttendeeState.REMOVED:
            return JoinDecision(JoinStatus.REFUSED, reason="removed")
        if attendee.state is AttendeeState.LOBBY:
            return JoinDecision(JoinStatus.LOBBY)
        return JoinDecision(JoinStatus.ADMITTED, token=self._token(attendee))

    def is_authenticated(self, device_id: str, secret: str) -> bool:
        return self._authenticate(device_id, secret) is not None

    def _permissions(self, attendee: Attendee) -> tuple[str, ...]:
        return PRESENT if attendee.part_holder else OBSERVE

    def _token(self, attendee: Attendee) -> str:
        return self._mint(attendee.name, self._permissions(attendee))

    # ── operator side ─────────────────────────────────────────────────────

    def admit(self, device_id: str) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.state is not AttendeeState.LOBBY:
            return False
        attendee.state = AttendeeState.ADMITTED
        attendee.admitted_at = self._clock()
        self._effects.notify(device_id, {"type": "admitted", "token": self._token(attendee), "name": attendee.name})
        self._effects.changed()
        return True

    def admit_all(self) -> int:
        waiting = [a.device_id for a in self.attendees.values() if a.state is AttendeeState.LOBBY]
        return sum(1 for device_id in waiting if self.admit(device_id))

    def remove(self, device_id: str) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.state is AttendeeState.REMOVED:
            return False
        attendee.state = AttendeeState.REMOVED
        attendee.hand_raised_at = None
        attendee.live = False
        self._effects.notify(device_id, {"type": "removed"})
        for galene_id in list(attendee.galene_ids):
            self._effects.kick(galene_id, "Removed from the meeting")
        self._effects.changed()
        return True

    def rename(self, device_id: str, name: str) -> bool:
        attendee = self.attendees.get(device_id)
        name = clean_name(name)
        if attendee is None or not name or attendee.state is AttendeeState.REMOVED:
            return False
        attendee.name = name
        if attendee.state is AttendeeState.ADMITTED:
            # Galene can't rename a user: the browser rejoins with a token in the new name.
            self._effects.notify(device_id, {"type": "renamed", "name": name, "token": self._token(attendee)})
        self._effects.changed()
        return True

    def set_part_holder(self, device_id: str, part_holder: bool) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.state is AttendeeState.REMOVED or attendee.part_holder == part_holder:
            return False
        attendee.part_holder = part_holder
        if attendee.state is AttendeeState.ADMITTED:
            for galene_id in attendee.galene_ids:
                if part_holder:
                    self._effects.present(galene_id)
                elif not attendee.live and self.mics_locked:
                    self._effects.unpresent(galene_id)
            self._effects.notify(device_id, {"type": "part_holder", "value": part_holder})
        self._effects.changed()
        return True

    def lock_mics(self, locked: bool) -> None:
        """Locked (during the meeting): only called-on commenters and part-holders speak."""

        if self.mics_locked == locked:
            return
        self.mics_locked = locked
        for attendee in self._admitted():
            if attendee.part_holder or attendee.live:
                continue
            for galene_id in attendee.galene_ids:
                (self._effects.unpresent if locked else self._effects.present)(galene_id)
        self._effects.changed()

    def call_on(self, device_id: str) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.state is not AttendeeState.ADMITTED or not attendee.connected:
            return False
        for other in self._admitted():
            if other.live and other.device_id != device_id:
                self.done(other.device_id)
        attendee.live = True
        self._lower(attendee)
        for galene_id in attendee.galene_ids:
            self._effects.present(galene_id)
        self._effects.notify(device_id, {"type": "called_on"})
        self._effects.changed()
        return True

    def done(self, device_id: str) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or not attendee.live:
            return False
        attendee.live = False
        if not attendee.part_holder and self.mics_locked:
            for galene_id in attendee.galene_ids:
                self._effects.unpresent(galene_id)
        self._effects.notify(device_id, {"type": "done"})
        self._effects.changed()
        return True

    def lower_hand(self, device_id: str) -> bool:
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.hand_raised_at is None:
            return False
        self._lower(attendee)
        self._effects.notify(device_id, {"type": "hand_lowered"})
        self._effects.changed()
        return True

    def end(self) -> None:
        if self.ended:
            return
        self.ended = True
        for attendee in self.attendees.values():
            if attendee.state is not AttendeeState.REMOVED:
                self._effects.notify(attendee.device_id, {"type": "ended"})
        self._effects.changed()

    # ── Galene side ───────────────────────────────────────────────────────

    def on_galene_user(self, kind: str, user: GaleneUser) -> None:
        """Track Galene clients: their ``device`` data maps them to attendees."""

        if kind == "delete":
            device_id = self._by_galene.pop(user.id, None)
            attendee = self.attendees.get(device_id or "")
            if attendee is not None:
                attendee.galene_ids.discard(user.id)
                if not attendee.connected:
                    attendee.live = False  # a commenter who drops out is no longer live
                self._effects.changed()
            return
        device_id = str(user.data.get("device") or "")
        attendee = self.attendees.get(device_id)
        if attendee is None or attendee.state is not AttendeeState.ADMITTED:
            if device_id and attendee is not None and attendee.state is AttendeeState.REMOVED:
                self._effects.kick(user.id, "Removed from the meeting")
            return
        new_client = user.id not in attendee.galene_ids
        self._by_galene[user.id] = device_id
        attendee.galene_ids.add(user.id)
        should_present = attendee.part_holder or attendee.live or not self.mics_locked
        if new_client and should_present != user.present:
            (self._effects.present if should_present else self._effects.unpresent)(user.id)
        elif not should_present and user.present:
            # Never let a client keep a microphone it was not given.
            self._effects.unpresent(user.id)
        self._track_hand(attendee, user.data.get("raisehand"))
        self._effects.changed()

    def _track_hand(self, attendee: Attendee, value: Any) -> None:
        if not value:
            attendee.hand_raised_at = attendee.hand_value = attendee.hand_lowered = None
            return
        if value == attendee.hand_lowered or attendee.live:
            return  # stale: the hall already lowered this raise (or called on them)
        if attendee.hand_raised_at is None or value != attendee.hand_value:
            if attendee.hand_raised_at is None:
                attendee.hand_raised_at = self._clock()
            attendee.hand_value = value

    def _lower(self, attendee: Attendee) -> None:
        if attendee.hand_value is not None:
            attendee.hand_lowered = attendee.hand_value
        attendee.hand_raised_at = attendee.hand_value = None

    # ── views ─────────────────────────────────────────────────────────────

    def _admitted(self) -> Iterable[Attendee]:
        return [a for a in self.attendees.values() if a.state is AttendeeState.ADMITTED]

    def hands(self) -> list[Attendee]:
        raised = [a for a in self._admitted() if a.hand_raised_at is not None]
        return sorted(raised, key=lambda a: a.hand_raised_at or 0.0)

    def snapshot(self) -> dict[str, Any]:
        def row(a: Attendee) -> dict[str, Any]:
            return {
                "device": a.device_id, "name": a.name, "state": a.state.value,
                "connected": a.connected, "live": a.live, "partHolder": a.part_holder,
                "hand": a.hand_raised_at is not None,
            }

        lobby = sorted((a for a in self.attendees.values() if a.state is AttendeeState.LOBBY),
                       key=lambda a: a.requested_at)
        admitted = sorted(self._admitted(), key=lambda a: a.name.casefold())
        return {
            "micsLocked": self.mics_locked,
            "ended": self.ended,
            "lobby": [row(a) for a in lobby],
            "attendees": [row(a) for a in admitted],
            "hands": [row(a) for a in self.hands()],
            "connected": sum(1 for a in admitted if a.connected),
            "attendance": sum(1 for a in self.attendees.values() if a.admitted_at is not None),
            "live": next((a.device_id for a in admitted if a.live), ""),
        }

    def attendance_report(self) -> list[dict[str, Any]]:
        """Everyone admitted during this meeting, in admission order (removed included)."""

        admitted = [a for a in self.attendees.values() if a.admitted_at is not None]
        return [{"name": a.name, "admittedAt": a.admitted_at, "removed": a.state is AttendeeState.REMOVED,
                 "connected": a.connected} for a in sorted(admitted, key=lambda a: a.admitted_at or 0.0)]

    def stage_view(self) -> dict[str, Any]:
        """What the conductor's stage page shows: hands in order and who is speaking."""

        live = next((a.name for a in self._admitted() if a.live), "")
        return {"hands": [a.name for a in self.hands()], "live": live, "ended": self.ended}
