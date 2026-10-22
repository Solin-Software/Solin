"""A small client for LiveKit's server API (Twirp, JSON over HTTP).

Only what meeting moderation needs: create/delete the room, list participants, update one
participant's permission, name or attributes, and remove one. Calls are blocking and meant
for a worker thread. Behaviour verified against livekit-server 1.13 (spike S9):

* ``UpdateParticipant`` *replaces* the whole permission, so :class:`Permission` is always
  sent complete;
* attributes *merge*, and an empty string deletes a key;
* ``can_publish_sources=[]`` means "every source", so locking uses ``can_publish=False``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from solin.core.network.http import HttpError, HttpStatusError, post_json

from .tokens import mint_admin_token

_SERVICE_PATH = "/twirp/livekit.RoomService/"


class TrackSource(StrEnum):
    CAMERA = "CAMERA"
    MICROPHONE = "MICROPHONE"
    SCREEN_SHARE = "SCREEN_SHARE"
    SCREEN_SHARE_AUDIO = "SCREEN_SHARE_AUDIO"


class RoomApiError(RuntimeError):
    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


class ParticipantNotFoundError(RoomApiError):
    pass


@dataclass(frozen=True, slots=True)
class Permission:
    can_subscribe: bool = False
    can_publish: bool = False
    can_publish_data: bool = False
    can_publish_sources: tuple[TrackSource, ...] = ()
    hidden: bool = False
    can_update_metadata: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "can_subscribe": self.can_subscribe,
            "can_publish": self.can_publish,
            "can_publish_data": self.can_publish_data,
            "can_publish_sources": [source.value for source in self.can_publish_sources],
            "hidden": self.hidden,
            "can_update_metadata": self.can_update_metadata,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any] | None) -> Permission:
        record = record or {}
        sources = tuple(
            TrackSource(value) for value in record.get("can_publish_sources") or ()
            if value in TrackSource.__members__
        )
        return cls(
            can_subscribe=bool(record.get("can_subscribe", False)),
            can_publish=bool(record.get("can_publish", False)),
            can_publish_data=bool(record.get("can_publish_data", False)),
            can_publish_sources=sources,
            hidden=bool(record.get("hidden", False)),
            can_update_metadata=bool(record.get("can_update_metadata", False)),
        )


@dataclass(frozen=True, slots=True)
class TrackInfo:
    sid: str
    kind: str  # "AUDIO" or "VIDEO"
    source: str
    muted: bool

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TrackInfo:
        return cls(
            sid=str(record.get("sid", "")),
            kind=str(record.get("type", "")),
            source=str(record.get("source", "")),
            muted=bool(record.get("muted", False)),
        )


@dataclass(frozen=True, slots=True)
class ParticipantInfo:
    identity: str
    name: str = ""
    state: str = "ACTIVE"
    joined_at_ms: int = 0
    permission: Permission = field(default_factory=Permission)
    attributes: Mapping[str, str] = field(default_factory=dict)
    tracks: tuple[TrackInfo, ...] = ()

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ParticipantInfo:
        try:
            joined = int(record.get("joined_at_ms") or 0)
        except (TypeError, ValueError):
            joined = 0
        attributes = record.get("attributes") or {}
        return cls(
            identity=str(record.get("identity", "")),
            name=str(record.get("name", "")),
            state=str(record.get("state", "")),
            joined_at_ms=joined,
            permission=Permission.from_record(record.get("permission")),
            attributes={str(k): str(v) for k, v in attributes.items()},
            tracks=tuple(TrackInfo.from_record(t) for t in record.get("tracks") or ()),
        )

    def publishing(self, source: TrackSource) -> bool:
        return any(track.source == source.value and not track.muted for track in self.tracks)


Poster = Callable[..., Any]


class RoomApi:
    def __init__(self, http_url: str, api_key: str, api_secret: str, *,
                 timeout: float = 3.0, post: Poster = post_json) -> None:
        self._base = http_url.rstrip("/") + _SERVICE_PATH
        self._key = api_key
        self._secret = api_secret
        self._timeout = timeout
        self._post = post

    def _call(self, method: str, room: str, body: Mapping[str, Any]) -> Any:
        token = mint_admin_token(self._key, self._secret, room)
        try:
            return self._post(self._base + method, dict(body), timeout=self._timeout,
                              headers={"Authorization": f"Bearer {token}"})
        except HttpStatusError as exc:
            code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
            error = ParticipantNotFoundError if code == "not_found" else RoomApiError
            raise error(f"{method} failed: {exc}", code) from exc
        except HttpError as exc:
            raise RoomApiError(f"{method} failed: {exc}") from exc

    def create_room(self, room: str, *, empty_timeout_s: int = 600,
                    departure_timeout_s: int = 20, metadata: str = "") -> None:
        self._call("CreateRoom", room, {
            "name": room, "empty_timeout": empty_timeout_s,
            "departure_timeout": departure_timeout_s, "metadata": metadata,
        })

    def delete_room(self, room: str) -> None:
        self._call("DeleteRoom", room, {"room": room})

    def update_room_metadata(self, room: str, metadata: str) -> None:
        self._call("UpdateRoomMetadata", room, {"room": room, "metadata": metadata})

    def list_participants(self, room: str) -> list[ParticipantInfo]:
        reply = self._call("ListParticipants", room, {"room": room}) or {}
        return [ParticipantInfo.from_record(p) for p in reply.get("participants") or ()]

    def update_participant(
        self, room: str, identity: str, *, permission: Permission | None = None,
        name: str | None = None, attributes: Mapping[str, str] | None = None,
    ) -> ParticipantInfo:
        body: dict[str, Any] = {"room": room, "identity": identity}
        if permission is not None:
            body["permission"] = permission.to_record()
        if name is not None:
            body["name"] = name
        if attributes:
            body["attributes"] = dict(attributes)
        return ParticipantInfo.from_record(self._call("UpdateParticipant", room, body) or {})

    def remove_participant(self, room: str, identity: str) -> None:
        self._call("RemoveParticipant", room, {"room": room, "identity": identity})
