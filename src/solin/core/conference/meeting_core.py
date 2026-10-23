"""The rules of a Solin-hosted meeting, independent of Qt, threads and the network.

:class:`MeetingCore` keeps the operator's intent (who is admitted, who may speak, whether
the meeting is locked, renames, removals) and compares it with what the server reports.
:meth:`MeetingCore.observe` takes a participant snapshot and returns the
:class:`MeetingAction` list that brings the room in line; operator commands change the
intent and return the actions they need right away. Whoever owns the core applies the
actions with :class:`~solin.core.conference.room_api.RoomApi`.

Rules (from the JW meeting guidelines and spike S9):

* attendees wait in a hidden lobby that hears nothing, until the operator admits them;
* an identity admitted once in this meeting is re-admitted automatically when it rejoins
  (a hall network blip must not strand everyone in the lobby);
* while the meeting is locked nobody can unmute themselves; the operator "calls on" one
  commenter, which grants the microphone only;
* removed identities are put out and must not be issued new tokens;
* hands are ordered by when Solin first saw them raised;
* attendance is the sum of each admitted device's ``attendance`` attribute.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .room_api import ParticipantInfo, Permission, TrackSource

HALL_IDENTITY = "hall"
ATTENDEE_PREFIX = "att:"
HAND_ATTRIBUTE = "hand"
ATTENDANCE_ATTRIBUTE = "attendance"
MAX_ATTENDANCE_PER_DEVICE = 50
RESEND_AFTER_S = 3.0


class ParticipantStatus(StrEnum):
    LOBBY = "lobby"
    ADMITTED = "admitted"
    SPEAKING = "speaking"


@dataclass(frozen=True, slots=True)
class SetPermission:
    identity: str
    permission: Permission


@dataclass(frozen=True, slots=True)
class SetName:
    identity: str
    name: str


@dataclass(frozen=True, slots=True)
class ClearHand:
    identity: str


@dataclass(frozen=True, slots=True)
class RemoveParticipant:
    identity: str


MeetingAction = SetPermission | SetName | ClearHand | RemoveParticipant


@dataclass(frozen=True, slots=True)
class MeetingParticipant:
    identity: str
    name: str
    status: ParticipantStatus
    attendance: int
    joined_at_ms: int
    hand_order: int | None = None  # position key while the hand is up
    microphone_live: bool = False
    spotlight: bool = False  # a remote part-holder shown in the hall and the program

    @property
    def hand_raised(self) -> bool:
        return self.hand_order is not None


@dataclass(frozen=True, slots=True)
class MeetingPolicy:
    attendee_video: bool = False


def is_attendee(identity: str) -> bool:
    return identity.startswith(ATTENDEE_PREFIX) and len(identity) > len(ATTENDEE_PREFIX)


def lobby_permission() -> Permission:
    # can_update_metadata lets a waiting attendee send name, attendance and hand.
    return Permission(hidden=True, can_update_metadata=True)


def _attendance(value: str | None) -> int:
    try:
        count = int(value or 1)
    except ValueError:
        return 1
    return max(1, min(MAX_ATTENDANCE_PER_DEVICE, count))


@dataclass(slots=True)
class _Sent:
    action: MeetingAction
    at: float


@dataclass(slots=True)
class _Hand:
    value: str
    order: int


@dataclass(slots=True)
class MeetingCore:
    policy: MeetingPolicy = field(default_factory=MeetingPolicy)
    clock: Callable[[], float] = time.monotonic
    locked: bool = False
    _admitted: set[str] = field(default_factory=set)
    _speaking: set[str] = field(default_factory=set)
    _removed: set[str] = field(default_factory=set)
    _names: dict[str, str] = field(default_factory=dict)
    _hands: dict[str, _Hand] = field(default_factory=dict)
    _hand_counter: int = 0
    _spotlight: str = ""
    _snapshot: dict[str, ParticipantInfo] = field(default_factory=dict)
    _sent: dict[tuple[str, str], _Sent] = field(default_factory=dict)

    # -- the server's view -------------------------------------------------------------
    def observe(self, snapshot: Iterable[ParticipantInfo]) -> list[MeetingAction]:
        self._snapshot = {p.identity: p for p in snapshot if is_attendee(p.identity)}
        # A new connection needs its permission now, even if one was sent moments ago.
        self._sent = {k: v for k, v in self._sent.items() if k[1] in self._snapshot}
        for identity in list(self._hands):
            participant = self._snapshot.get(identity)
            value = participant.attributes.get(HAND_ATTRIBUTE, "") if participant else ""
            if not value:
                del self._hands[identity]
        for identity, participant in self._snapshot.items():
            value = participant.attributes.get(HAND_ATTRIBUTE, "")
            hand = self._hands.get(identity)
            if value and (hand is None or hand.value != value):
                self._hand_counter += 1
                self._hands[identity] = _Hand(value, self._hand_counter)
        return self._reconcile()

    # -- operator commands ---------------------------------------------------------------
    def admit(self, identity: str) -> list[MeetingAction]:
        if is_attendee(identity) and identity not in self._removed:
            self._admitted.add(identity)
        return self._reconcile()

    def admit_all(self) -> list[MeetingAction]:
        for identity in self._snapshot:
            if identity not in self._removed:
                self._admitted.add(identity)
        return self._reconcile()

    def to_lobby(self, identity: str) -> list[MeetingAction]:
        self._admitted.discard(identity)
        self._speaking.discard(identity)
        if self._spotlight == identity:
            self._spotlight = ""
        return self._reconcile()

    def spotlight(self, identity: str) -> list[MeetingAction]:
        """Show one admitted remote part-holder: camera and microphone, even when locked."""
        if identity in self._admitted and identity not in self._removed:
            self._spotlight = identity
        return self._reconcile()

    def clear_spotlight(self) -> list[MeetingAction]:
        self._spotlight = ""
        return self._reconcile()

    @property
    def spotlighted(self) -> str:
        return self._spotlight

    def call_on(self, identity: str) -> list[MeetingAction]:
        """Give one admitted attendee the microphone while the meeting is locked."""
        if identity in self._admitted:
            self._speaking.add(identity)
        return self._reconcile()

    def finish(self, identity: str) -> list[MeetingAction]:
        """End a comment: take the microphone back and lower the hand."""
        self._speaking.discard(identity)
        return self._lower(identity) + self._reconcile()

    def lower_hand(self, identity: str) -> list[MeetingAction]:
        return self._lower(identity)

    def rename(self, identity: str, name: str) -> list[MeetingAction]:
        name = name.strip()
        if not name:
            self._names.pop(identity, None)
            return []
        self._names[identity] = name[:64]
        return self._reconcile()

    def remove(self, identity: str) -> list[MeetingAction]:
        if not is_attendee(identity):
            return []
        self._removed.add(identity)
        self._admitted.discard(identity)
        self._speaking.discard(identity)
        if self._spotlight == identity:
            self._spotlight = ""
        return self._reconcile()

    def set_locked(self, locked: bool) -> list[MeetingAction]:
        self.locked = locked
        if not locked:
            self._speaking.clear()
        return self._reconcile()

    def removed(self) -> frozenset[str]:
        return frozenset(self._removed)

    def may_join(self, identity: str) -> bool:
        """Whether the token issuer may hand ``identity`` a token for this meeting."""
        return is_attendee(identity) and identity not in self._removed

    # -- views ---------------------------------------------------------------------------
    def participants(self) -> list[MeetingParticipant]:
        result = [self._view(p) for p in self._snapshot.values() if p.identity not in self._removed]
        return sorted(result, key=lambda p: (p.status != ParticipantStatus.LOBBY, p.joined_at_ms))

    def hands(self) -> list[MeetingParticipant]:
        raised = [p for p in self.participants() if p.hand_raised and p.status != ParticipantStatus.LOBBY]
        return sorted(raised, key=lambda p: p.hand_order or 0)

    def attendance(self) -> int:
        return sum(p.attendance for p in self.participants() if p.status != ParticipantStatus.LOBBY)

    # -- internals ----------------------------------------------------------------------
    def _status(self, identity: str) -> ParticipantStatus:
        if identity not in self._admitted:
            return ParticipantStatus.LOBBY
        return ParticipantStatus.SPEAKING if identity in self._speaking else ParticipantStatus.ADMITTED

    def _view(self, participant: ParticipantInfo) -> MeetingParticipant:
        hand = self._hands.get(participant.identity)
        return MeetingParticipant(
            identity=participant.identity,
            name=self._names.get(participant.identity, participant.name),
            status=self._status(participant.identity),
            attendance=_attendance(participant.attributes.get(ATTENDANCE_ATTRIBUTE)),
            joined_at_ms=participant.joined_at_ms,
            hand_order=hand.order if hand else None,
            microphone_live=participant.publishing(TrackSource.MICROPHONE),
            spotlight=participant.identity == self._spotlight,
        )

    def _desired_permission(self, identity: str) -> Permission:
        status = self._status(identity)
        if status is ParticipantStatus.LOBBY:
            return lobby_permission()
        if identity == self._spotlight:
            return Permission(can_subscribe=True, can_publish=True,
                              can_publish_sources=(TrackSource.CAMERA, TrackSource.MICROPHONE),
                              can_update_metadata=True)
        if self.locked and status is not ParticipantStatus.SPEAKING:
            return Permission(can_subscribe=True, can_update_metadata=True)
        sources = (TrackSource.MICROPHONE,)
        if self.policy.attendee_video and not self.locked:
            sources = (TrackSource.CAMERA, TrackSource.MICROPHONE)
        # Never an empty source list: LiveKit reads [] as "every source".
        return Permission(can_subscribe=True, can_publish=True, can_publish_sources=sources,
                          can_update_metadata=True)

    def _lower(self, identity: str) -> list[MeetingAction]:
        if self._hands.pop(identity, None) is None:
            return []
        return [ClearHand(identity)]

    def _reconcile(self) -> list[MeetingAction]:
        actions: list[MeetingAction] = []
        for identity, participant in self._snapshot.items():
            if identity in self._removed:
                actions.append(RemoveParticipant(identity))
                continue
            desired = self._desired_permission(identity)
            if participant.permission != desired:
                actions.append(SetPermission(identity, desired))
            name = self._names.get(identity)
            if name and participant.name != name:
                actions.append(SetName(identity, name))
        return self._throttle(actions)

    def _throttle(self, actions: list[MeetingAction]) -> list[MeetingAction]:
        now = self.clock()
        self._sent = {k: v for k, v in self._sent.items() if now - v.at < RESEND_AFTER_S}
        fresh: list[MeetingAction] = []
        for action in actions:
            key = (type(action).__name__, action.identity)
            sent = self._sent.get(key)
            if sent is not None and sent.action == action:
                continue  # the server has not caught up yet
            self._sent[key] = _Sent(action, now)
            fresh.append(action)
        return fresh
