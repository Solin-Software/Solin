"""Who may join the meeting, as one immutable snapshot the attendee web server can read.

The meeting service owns the rules (:class:`~solin.core.conference.meeting_core.MeetingCore`)
on the Qt thread; the web server runs on its own thread. Instead of calling across, the
service publishes an :class:`AttendeeAccess` whenever something relevant changes and the
server mints lobby tokens from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .meeting_core import ATTENDEE_PREFIX
from .tokens import AccessTokenRequest, RoomGrants, mint_access_token

ATTENDEE_TOKEN_TTL_S = 15 * 60
MAX_NAME_LENGTH = 64
_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


@dataclass(frozen=True, slots=True)
class AttendeeAccess:
    meeting_id: str
    hall_name: str
    room: str
    api_key: str
    api_secret: str
    stage_secret: str
    removed: frozenset[str] = field(default_factory=frozenset)
    e2ee: bool = False  # whether the join link must carry the key (never the key itself)


@dataclass(frozen=True, slots=True)
class StageHand:
    name: str
    position: int
    speaking: bool


class JoinRefusedError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def valid_device_id(device_id: object) -> bool:
    return isinstance(device_id, str) and _DEVICE_ID.fullmatch(device_id) is not None


def clean_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    printable = "".join(ch for ch in name if ch.isprintable())
    return " ".join(printable.split())[:MAX_NAME_LENGTH]


def attendee_token(access: AttendeeAccess, device_id: str, name: str = "", *,
                   now: float | None = None) -> str:
    """A short-lived lobby token: hidden, hears nothing, may only set its own attributes."""
    if not valid_device_id(device_id):
        raise JoinRefusedError("invalid_device")
    identity = ATTENDEE_PREFIX + device_id
    if identity in access.removed:
        raise JoinRefusedError("removed")
    grants = RoomGrants(access.room, can_publish=False, can_subscribe=False,
                        can_publish_data=False, hidden=True, can_update_own_metadata=True)
    return mint_access_token(access.api_key, access.api_secret, AccessTokenRequest(
        identity, grants, name=clean_name(name), ttl_seconds=ATTENDEE_TOKEN_TTL_S,
    ), now=now)
