"""The meeting's rules, independent of Qt, the web server and Galene's wire format.

Attendees are identified by a *device*: a random id plus a secret the browser keeps in
``localStorage``. The core decides who waits in the lobby, who is admitted (and gets
short-lived Galene join tokens), whose microphone is open, the raised-hands queue and
the attendance count. It talks to the outside through :class:`CoreEffects`:

* ``notify(device, message)`` — push a message to that browser (lobby → admitted,
  hand lowered, renamed, removed, meeting ended);
* ``notify_staff(device, message)`` — the same for stage and moderator pages;
* ``present`` / ``unpresent`` / ``kick`` — Galene moderation on a Galene client id;
* ``changed()`` — the operator-facing snapshot changed;
* ``devices_changed(json)`` — the approved stage/moderator devices changed (persist them).

Meeting rules (JW guidance): attendees cannot unmute themselves while the microphones
are locked; the operator calls on one commenter at a time (microphone only); a raised
hand is lowered when called on; remote part-holders may present camera and microphone;
removed devices get no new tokens.

Access: every page asks for the meeting password. Attendees also say how many people
watch on their device (0 = "already counted"); the number is fixed once given. Stage
devices and remote moderators are approved by the operator (never by a moderator),
then remembered (:mod:`.devices`). Moderators have the
operator's power over people — lobby, calling on, microphones, hands, removal — but not
over the program, the links or the end of the meeting.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .devices import ApprovedDevice, DeviceRegistry, KeyKind, StaffRole, load_public_key, new_challenge, secret_hash
from .galene_client import GaleneUser
from .tokens import OBSERVE, PRESENT

MAX_NAME_LENGTH = 60
MAX_LOBBY = 500
MAX_COUNT = 20
MAX_PENDING_STAFF = 20
# Someone whose connection dropped still counts for this long (a bad network, a reload).
RECONNECT_GRACE_S = 180.0
# What a moderator may do: the operator's power over people, nothing else.
MODERATOR_COMMANDS = frozenset({"admit", "admit_all", "remove", "call_on", "done", "lower_hand", "lock_mics"})


class AttendeeState(StrEnum):
    LOBBY = "lobby"
    ADMITTED = "admitted"
    REMOVED = "removed"


class JoinStatus(StrEnum):
    LOBBY = "lobby"
    ADMITTED = "admitted"
    REFUSED = "refused"
    ENDED = "ended"


class StaffStatus(StrEnum):
    READY = "ready"            # approved and verified: the page may be used
    CHALLENGE = "challenge"    # sign this challenge with the device key
    LOGIN = "login"            # unknown device: ask for name and password
    PENDING = "pending"        # waiting for the operator's approval
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class JoinDecision:
    status: JoinStatus
    token: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StaffDecision:
    status: StaffStatus
    challenge: str = ""
    reason: str = ""


class CoreEffects(Protocol):
    def notify(self, device_id: str, message: dict[str, Any]) -> None: ...
    def notify_staff(self, device_id: str, message: dict[str, Any]) -> None: ...
    def present(self, galene_id: str) -> None: ...
    def unpresent(self, galene_id: str) -> None: ...
    def kick(self, galene_id: str, reason: str) -> None: ...
    def changed(self) -> None: ...
    def devices_changed(self, devices: list[dict[str, Any]]) -> None: ...


TokenMinter = Callable[[str, tuple[str, ...]], str]  # (username, permissions) -> token


@dataclass(slots=True)
class Attendee:
    device_id: str
    name: str
    secret_hash: str
    count: int = 1
    state: AttendeeState = AttendeeState.LOBBY
    requested_at: float = 0.0
    admitted_at: float | None = None
    part_holder: bool = False
    galene_ids: set[str] = field(default_factory=set)
    disconnected_at: float | None = None
    hand_raised_at: float | None = None
    hand_value: Any = None      # the client's raisehand value currently counted as raised
    hand_lowered: Any = None    # a value the hall lowered: ignored until the client sends a new one
    live: bool = False  # the operator called on them (microphone open)

    @property
    def connected(self) -> bool:
        return bool(self.galene_ids)


@dataclass(slots=True)
class StaffMember:
    """A stage device or moderator during this meeting (pending, or approved and online)."""

    device_id: str
    role: StaffRole
    name: str
    network: str = ""
    pending: bool = True
    key_kind: KeyKind = KeyKind.SECRET
    key: str = ""
    requested_at: float = 0.0
    challenge: str = ""
    sockets: int = 0
    network_changed: bool = False


def clean_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    return cleaned[:MAX_NAME_LENGTH]


def clean_count(value: Any) -> int | None:
    """0 ("already counted") to :data:`MAX_COUNT`; None when missing or invalid."""

    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 0 <= count <= MAX_COUNT else None


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class MeetingCore:
    def __init__(
        self,
        effects: CoreEffects,
        mint: TokenMinter,
        *,
        clock: Callable[[], float] = time.time,
        password: str = "",
        devices: DeviceRegistry | None = None,
    ) -> None:
        self._effects = effects
        self._mint = mint
        self._clock = clock
        self.password = password
        self.devices = devices or DeviceRegistry()
        self.attendees: dict[str, Attendee] = {}
        self.staff: dict[str, StaffMember] = {}
        self._by_galene: dict[str, str] = {}
        self.mics_locked = True
        self.ended = False
        self.official_count: dict[str, Any] | None = None

    # ── password ──────────────────────────────────────────────────────────

    def password_ok(self, password: str) -> bool:
        if not self.password:
            return True
        return hmac.compare_digest(str(password or "").strip().encode("utf-8"), self.password.encode("utf-8"))

    def set_password(self, password: str) -> None:
        """A new password: people already admitted stay, new arrivals need it."""

        self.password = password

    # ── browser side (attendees) ──────────────────────────────────────────

    def _authenticate(self, device_id: str, secret: str) -> Attendee | None:
        attendee = self.attendees.get(device_id)
        if attendee is None or not hmac.compare_digest(attendee.secret_hash, _hash(secret)):
            return None
        return attendee

    def request_join(
        self, device_id: str, secret: str, name: str, *, password: str = "", count: Any = 1
    ) -> JoinDecision:
        if self.ended:
            return JoinDecision(JoinStatus.ENDED)
        if not device_id or len(secret) < 16:
            return JoinDecision(JoinStatus.REFUSED, reason="invalid device")
        attendee = self.attendees.get(device_id)
        if attendee is not None:
            if not hmac.compare_digest(attendee.secret_hash, _hash(secret)):
                return JoinDecision(JoinStatus.REFUSED, reason="invalid device")
            if attendee.state is AttendeeState.REMOVED:
                return JoinDecision(JoinStatus.REFUSED, reason="removed")
            if attendee.state is AttendeeState.ADMITTED:
                # Same device coming back (reload, network change, Solin restarted): straight in.
                return JoinDecision(JoinStatus.ADMITTED, token=self._token(attendee))
            # Already waiting (the page reloaded): the password was checked when it first asked.
            return JoinDecision(JoinStatus.LOBBY)
        if not self.password_ok(password):
            return JoinDecision(JoinStatus.REFUSED, reason="password")
        name = clean_name(name)
        if not name:
            return JoinDecision(JoinStatus.REFUSED, reason="name required")
        people = clean_count(count)
        if people is None:
            return JoinDecision(JoinStatus.REFUSED, reason="count required")
        if sum(1 for a in self.attendees.values() if a.state is AttendeeState.LOBBY) >= MAX_LOBBY:
            return JoinDecision(JoinStatus.REFUSED, reason="lobby full")
        self.attendees[device_id] = Attendee(
            device_id=device_id, name=name, secret_hash=_hash(secret), count=people, requested_at=self._clock(),
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
        for member in self.staff.values():
            self._effects.notify_staff(member.device_id, {"type": "ended"})
        self._effects.changed()

    # ── stage devices and moderators ──────────────────────────────────────

    def staff_hello(self, device_id: str, role: str, secret: str, network: str) -> StaffDecision:
        """A stage or moderator page opened: known devices skip the password and approval."""

        if self.ended:
            return StaffDecision(StaffStatus.REFUSED, reason="ended")
        try:
            staff_role = StaffRole(role)
        except ValueError:
            return StaffDecision(StaffStatus.REFUSED, reason="invalid role")
        if not device_id:
            return StaffDecision(StaffStatus.REFUSED, reason="invalid device")
        now = self._clock()
        member = self.staff.get(device_id)
        if member is not None and member.pending and member.role is staff_role:
            member.network = network or member.network
            return StaffDecision(StaffStatus.PENDING)
        device = self.devices.get(device_id, now)
        if device is None or device.role is not staff_role:
            return StaffDecision(StaffStatus.LOGIN)
        if device.key_kind is KeyKind.ECDSA:
            challenge = new_challenge()
            self.staff[device_id] = StaffMember(device_id, staff_role, device.name, network=network, pending=False,
                                                key_kind=KeyKind.ECDSA, key=device.key, challenge=challenge,
                                                sockets=self._sockets(device_id))
            return StaffDecision(StaffStatus.CHALLENGE, challenge=challenge)
        if self.devices.check_secret(device_id, secret, now) is None:
            return StaffDecision(StaffStatus.LOGIN)
        self._staff_ready(device, network)
        return StaffDecision(StaffStatus.READY)

    def staff_proof(self, device_id: str, signature: str) -> StaffDecision:
        """The signed challenge from a moderator device (see :func:`.devices.verify_signature`)."""

        member = self.staff.get(device_id)
        now = self._clock()
        if member is None or not member.challenge:
            return StaffDecision(StaffStatus.LOGIN)
        challenge, member.challenge = member.challenge, ""
        device = self.devices.check_signature(device_id, challenge, signature, now)
        if device is None:
            del self.staff[device_id]
            return StaffDecision(StaffStatus.LOGIN, reason="signature")
        self._staff_ready(device, member.network)
        return StaffDecision(StaffStatus.READY)

    def staff_login(
        self, device_id: str, role: str, name: str, password: str, *, key_kind: str, key: str, network: str
    ) -> StaffDecision:
        """An unknown device asks to become a stage device or a moderator."""

        if self.ended:
            return StaffDecision(StaffStatus.REFUSED, reason="ended")
        try:
            staff_role = StaffRole(role)
            kind = KeyKind(key_kind)
        except ValueError:
            return StaffDecision(StaffStatus.REFUSED, reason="invalid request")
        name = clean_name(name)
        if not device_id or not name:
            return StaffDecision(StaffStatus.REFUSED, reason="name required")
        if not self.password_ok(password):
            return StaffDecision(StaffStatus.LOGIN, reason="password")
        if kind is KeyKind.ECDSA and load_public_key(key) is None:
            return StaffDecision(StaffStatus.REFUSED, reason="invalid key")
        if kind is KeyKind.SECRET:
            if len(key) < 16:
                return StaffDecision(StaffStatus.REFUSED, reason="invalid key")
            key = secret_hash(key)
        if device_id not in self.staff and sum(1 for m in self.staff.values() if m.pending) >= MAX_PENDING_STAFF:
            return StaffDecision(StaffStatus.REFUSED, reason="too many requests")
        self.staff[device_id] = StaffMember(device_id, staff_role, name, network=network, pending=True,
                                            key_kind=kind, key=key, requested_at=self._clock(),
                                            sockets=self._sockets(device_id))
        self._effects.changed()
        return StaffDecision(StaffStatus.PENDING)

    def approve_staff(self, device_id: str) -> bool:
        """The operator approves a pending device: it is remembered from now on."""

        member = self.staff.get(device_id)
        if member is None or not member.pending:
            return False
        now = self._clock()
        device = ApprovedDevice(device_id, member.role, member.name, member.key_kind, member.key,
                                approved_at=now, last_seen=now, last_network=member.network)
        self.devices.approve(device)
        self._devices_changed()
        member.pending = False
        if member.key_kind is KeyKind.ECDSA:
            # Prove the key before showing anything: approval alone is not possession.
            member.challenge = new_challenge()
            self._effects.notify_staff(device_id, {"type": "challenge", "challenge": member.challenge})
        else:
            self._effects.notify_staff(device_id, {"type": "ready"})
        self._effects.changed()
        return True

    def refuse_staff(self, device_id: str) -> bool:
        member = self.staff.get(device_id)
        if member is None or not member.pending:
            return False
        del self.staff[device_id]
        self._effects.notify_staff(device_id, {"type": "refused"})
        self._effects.changed()
        return True

    def remove_device(self, device_id: str) -> bool:
        """Forget an approved device; if it is online, its page closes at once."""

        removed = self.devices.remove(device_id)
        if removed:
            self._devices_changed()
        if self.staff.pop(device_id, None) is not None:
            self._effects.notify_staff(device_id, {"type": "removed"})
            removed = True
        if removed:
            self._effects.changed()
        return removed

    def forget_all_devices(self) -> None:
        """New link: every approved device must be approved again."""

        for device_id in list(self.staff):
            self._effects.notify_staff(device_id, {"type": "removed"})
        self.staff.clear()
        self.devices.clear()
        self._devices_changed()
        self._effects.changed()

    def staff_socket(self, device_id: str, opened: bool) -> None:
        member = self.staff.get(device_id)
        if member is None:
            return
        member.sockets = max(0, member.sockets + (1 if opened else -1))
        if not opened and member.sockets == 0 and member.pending:
            del self.staff[device_id]  # gave up waiting
        self._effects.changed()

    def staff_role(self, device_id: str) -> StaffRole | None:
        """The role of a verified, approved device that is online now."""

        member = self.staff.get(device_id)
        if member is None or member.pending or member.challenge:
            return None
        return member.role

    def moderator_command(self, device_id: str, name: str, args: Iterable[Any]) -> bool:
        if self.staff_role(device_id) is not StaffRole.MODERATOR or name not in MODERATOR_COMMANDS:
            return False
        args = list(args)
        if name == "lock_mics":
            self.lock_mics(bool(args[0]) if args else True)
            return True
        if name == "admit_all":
            self.admit_all()
            return True
        if len(args) != 1 or not isinstance(args[0], str):
            return False
        return bool(getattr(self, name)(args[0]))

    def _staff_ready(self, device: ApprovedDevice, network: str) -> None:
        now = self._clock()
        changed_network = self.devices.seen(device.device_id, network, now)
        self._devices_changed()
        self.staff[device.device_id] = StaffMember(
            device.device_id, device.role, device.name, network=network, pending=False,
            key_kind=device.key_kind, key=device.key, network_changed=changed_network,
            sockets=self._sockets(device.device_id),
        )
        self._effects.changed()

    def _sockets(self, device_id: str) -> int:
        member = self.staff.get(device_id)
        return member.sockets if member is not None else 0

    def _devices_changed(self) -> None:
        self._effects.devices_changed(self.devices.to_json())

    # ── attendance ────────────────────────────────────────────────────────

    def _counted(self, attendee: Attendee, now: float) -> bool:
        if attendee.state is not AttendeeState.ADMITTED:
            return False
        if attendee.connected:
            return True
        since = attendee.disconnected_at if attendee.disconnected_at is not None else attendee.admitted_at
        return since is not None and now - since < RECONNECT_GRACE_S

    def totals(self) -> dict[str, int]:
        """People watching now (each device's count) and the devices they watch on."""

        now = self._clock()
        counted = [a for a in self.attendees.values() if self._counted(a, now)]
        return {"people": sum(a.count for a in counted), "devices": len(counted)}

    def take_official_count(self, at: str) -> dict[str, Any]:
        """The official attendance, taken at 0.75 of the meeting; moderators see it too."""

        self.official_count = {**self.totals(), "at": at}
        for member in self.staff.values():
            if member.role is StaffRole.MODERATOR and not member.pending:
                self._effects.notify_staff(member.device_id, {"type": "official_count", **self.official_count})
        self._effects.changed()
        return dict(self.official_count)

    # ── Solin restarts during a meeting ───────────────────────────────────

    def export_admitted(self) -> list[dict[str, Any]]:
        """Who was admitted, so a restarted Solin lets them straight back in."""

        return [{"device": a.device_id, "secret": a.secret_hash, "name": a.name, "count": a.count,
                 "partHolder": a.part_holder}
                for a in self.attendees.values() if a.state is AttendeeState.ADMITTED]

    def restore_admitted(self, rows: Iterable[Any]) -> int:
        restored = 0
        now = self._clock()
        for row in rows:
            if not isinstance(row, dict):
                continue
            device_id, secret, name = str(row.get("device") or ""), str(row.get("secret") or ""), clean_name(
                str(row.get("name") or ""))
            count = clean_count(row.get("count"))
            if not device_id or len(secret) != 64 or not name or count is None or device_id in self.attendees:
                continue
            self.attendees[device_id] = Attendee(
                device_id=device_id, name=name, secret_hash=secret, count=count, state=AttendeeState.ADMITTED,
                requested_at=now, admitted_at=now, part_holder=bool(row.get("partHolder")), disconnected_at=now,
            )
            restored += 1
        if restored:
            self._effects.changed()
        return restored

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
                    attendee.disconnected_at = self._clock()
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
        attendee.disconnected_at = None
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

    def _staff_rows(self) -> list[dict[str, Any]]:
        online = [m for m in self.staff.values() if not m.pending and not m.challenge and m.sockets > 0]
        return [{"device": m.device_id, "name": m.name, "role": m.role.value, "networkChanged": m.network_changed}
                for m in sorted(online, key=lambda m: (m.role.value, m.name.casefold()))]

    def snapshot(self) -> dict[str, Any]:
        def row(a: Attendee) -> dict[str, Any]:
            return {
                "device": a.device_id, "name": a.name, "state": a.state.value, "count": a.count,
                "connected": a.connected, "live": a.live, "partHolder": a.part_holder,
                "hand": a.hand_raised_at is not None,
            }

        lobby = sorted((a for a in self.attendees.values() if a.state is AttendeeState.LOBBY),
                       key=lambda a: a.requested_at)
        admitted = sorted(self._admitted(), key=lambda a: a.name.casefold())
        pending = sorted((m for m in self.staff.values() if m.pending), key=lambda m: m.requested_at)
        now = self._clock()
        return {
            "micsLocked": self.mics_locked,
            "ended": self.ended,
            "lobby": [row(a) for a in lobby],
            "attendees": [row(a) for a in admitted],
            "hands": [row(a) for a in self.hands()],
            "connected": sum(1 for a in admitted if a.connected),
            **self.totals(),
            "live": next((a.device_id for a in admitted if a.live), ""),
            "staffRequests": [{"device": m.device_id, "name": m.name, "role": m.role.value} for m in pending],
            "staff": self._staff_rows(),
            "approvedDevices": [{"device": d.device_id, "name": d.name, "role": d.role.value,
                                 "lastSeen": d.last_seen, "online": d.device_id in self.staff
                                 and self.staff[d.device_id].sockets > 0}
                                for d in self.devices.all(now)],
            "officialCount": dict(self.official_count) if self.official_count else None,
        }

    def moderator_view(self) -> dict[str, Any]:
        """What a remote moderator's page shows (no approved-device list, no pending staff)."""

        snapshot = self.snapshot()
        snapshot.pop("approvedDevices", None)
        snapshot.pop("staffRequests", None)
        return snapshot

    def stage_view(self) -> dict[str, Any]:
        """The read-only stage page: who is present, raised hands in order, who speaks."""

        live = next((a.name for a in self._admitted() if a.live), "")
        participants = [a.name for a in sorted(self._admitted(), key=lambda a: a.name.casefold())]
        return {"hands": [a.name for a in self.hands()], "live": live, "participants": participants,
                "staff": self._staff_rows(), "ended": self.ended}
