"""Framework-independent contracts for Solin's local remote control.

The objects in this module are the boundary between the authoritative desktop
runtime and network adapters.  They deliberately contain only public IDs and
display metadata: media locations, URLs, and persisted references must never
cross this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import ClassVar, TypeAlias
import uuid


JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

MAX_IDENTIFIER_LENGTH = 512
MAX_DISPLAY_TEXT_LENGTH = 4_096
MAX_ERROR_TEXT_LENGTH = 1_024


def _require_string(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
    maximum: int = MAX_DISPLAY_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    return value


def _require_identifier(value: str, *, field_name: str) -> str:
    return _require_string(
        value,
        field_name=field_name,
        maximum=MAX_IDENTIFIER_LENGTH,
    )


def _require_uuid(value: str, *, field_name: str) -> str:
    _require_identifier(value, field_name=field_name)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"{field_name} must use the canonical UUID representation")
    return value


def _require_nonnegative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return value


def _require_optional_nonnegative_int(value: int | None, *, field_name: str) -> None:
    if value is not None:
        _require_nonnegative_int(value, field_name=field_name)


class ProjectionSource(StrEnum):
    PLAYLIST = "playlist"
    LINKED_FOLDER = "linked_folder"
    MEETING = "meeting"
    TEMPORARY = "temporary"


class CatalogKind(StrEnum):
    PLAYLIST = "playlist"
    LINKED_FOLDER = "linked_folder"
    MEETING = "meeting"


class RemoteMeetingType(StrEnum):
    MIDWEEK = "midweek"
    WEEKEND = "weekend"
    MEMORIAL = "memorial"
    OTHER = "other"


class CatalogNodeKind(StrEnum):
    SECTION = "section"
    SUBSECTION = "subsection"
    MARKER = "marker"
    MEDIA = "media"


class RemoteMediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"
    BROWSER = "browser"
    ANNOUNCEMENT = "announcement"
    SCREEN = "screen"
    UNKNOWN = "unknown"


class PlaybackState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProjectionOrigin:
    """Stable public identity for a catalog item or temporary projection."""

    source: ProjectionSource
    collection_id: str | None = None
    node_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, ProjectionSource):
            raise TypeError("source must be a ProjectionSource")
        if self.source is ProjectionSource.TEMPORARY:
            if self.collection_id is not None:
                _require_identifier(self.collection_id, field_name="collection_id")
            if self.node_id is not None:
                _require_identifier(self.node_id, field_name="node_id")
            return
        if self.collection_id is None or self.node_id is None:
            raise ValueError("catalog projection origins require collection_id and node_id")
        _require_identifier(self.collection_id, field_name="collection_id")
        _require_identifier(self.node_id, field_name="node_id")

    def to_dict(self) -> JsonObject:
        return {
            "source": self.source.value,
            "collectionId": self.collection_id,
            "nodeId": self.node_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProjectionOrigin:
        if not isinstance(value, Mapping):
            raise TypeError("origin must be an object")
        _reject_unknown_fields(value, {"source", "collectionId", "nodeId"})
        try:
            source = ProjectionSource(value.get("source"))
        except (TypeError, ValueError) as exc:
            raise ValueError("origin.source is invalid") from exc
        collection_id = value.get("collectionId")
        node_id = value.get("nodeId")
        if collection_id is not None and not isinstance(collection_id, str):
            raise TypeError("origin.collectionId must be a string or null")
        if node_id is not None and not isinstance(node_id, str):
            raise TypeError("origin.nodeId must be a string or null")
        return cls(source=source, collection_id=collection_id, node_id=node_id)


@dataclass(frozen=True, slots=True)
class PlaybackCapabilities:
    can_pause: bool = False
    can_resume: bool = False
    can_seek: bool = False
    can_set_volume: bool = False
    can_previous: bool = False
    can_next: bool = False
    can_stop: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "can_pause",
            "can_resume",
            "can_seek",
            "can_set_volume",
            "can_previous",
            "can_next",
            "can_stop",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")

    def to_dict(self) -> JsonObject:
        return {
            "pause": self.can_pause,
            "resume": self.can_resume,
            "seek": self.can_seek,
            "setVolume": self.can_set_volume,
            "previous": self.can_previous,
            "next": self.can_next,
            "stop": self.can_stop,
        }


@dataclass(frozen=True, slots=True)
class ProjectionError:
    code: str
    message: str

    def __post_init__(self) -> None:
        _require_identifier(self.code, field_name="code")
        _require_string(
            self.message,
            field_name="message",
            maximum=MAX_ERROR_TEXT_LENGTH,
        )

    def to_dict(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class CatalogNode:
    id: str
    kind: CatalogNodeKind
    title: str
    children: tuple[CatalogNode, ...] = ()
    color: str | None = None
    collapsed: bool = False
    media_kind: RemoteMediaKind | None = None
    duration_ms: int | None = None
    thumbnail_id: str | None = None
    available: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.id, field_name="id")
        if not isinstance(self.kind, CatalogNodeKind):
            raise TypeError("kind must be a CatalogNodeKind")
        _require_string(self.title, field_name="title")
        if not isinstance(self.children, tuple) or not all(
            isinstance(child, CatalogNode) for child in self.children
        ):
            raise TypeError("children must be a tuple of CatalogNode objects")
        if self.kind is CatalogNodeKind.MEDIA and self.children:
            raise ValueError("media nodes cannot contain children")
        if self.color is not None:
            _require_string(self.color, field_name="color")
        if not isinstance(self.collapsed, bool):
            raise TypeError("collapsed must be a boolean")
        if self.collapsed and self.kind not in (
            CatalogNodeKind.SECTION,
            CatalogNodeKind.SUBSECTION,
        ):
            raise ValueError("only section nodes can be collapsed")
        if self.media_kind is not None and not isinstance(self.media_kind, RemoteMediaKind):
            raise TypeError("media_kind must be a RemoteMediaKind or None")
        _require_optional_nonnegative_int(self.duration_ms, field_name="duration_ms")
        if self.thumbnail_id is not None:
            _require_identifier(self.thumbnail_id, field_name="thumbnail_id")
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")
        if self.unavailable_reason is not None:
            _require_string(self.unavailable_reason, field_name="unavailable_reason")
        if self.available and self.unavailable_reason is not None:
            raise ValueError("available nodes cannot have an unavailable_reason")

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "children": [child.to_dict() for child in self.children],
            "color": self.color,
            "collapsed": self.collapsed,
            "mediaKind": self.media_kind.value if self.media_kind is not None else None,
            "durationMs": self.duration_ms,
            "thumbnailId": self.thumbnail_id,
            "available": self.available,
            "unavailableReason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class CatalogCollection:
    id: str
    kind: CatalogKind
    title: str
    nodes: tuple[CatalogNode, ...] = ()
    subtitle: str = ""
    thumbnail_id: str | None = None
    week_start: date | None = None
    meeting_type: RemoteMeetingType | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.id, field_name="id")
        if not isinstance(self.kind, CatalogKind):
            raise TypeError("kind must be a CatalogKind")
        _require_string(self.title, field_name="title")
        _require_string(self.subtitle, field_name="subtitle", allow_empty=True)
        if self.thumbnail_id is not None:
            _require_identifier(self.thumbnail_id, field_name="thumbnail_id")
        if self.week_start is not None and not isinstance(self.week_start, date):
            raise TypeError("week_start must be a date or None")
        if self.meeting_type is not None and not isinstance(self.meeting_type, RemoteMeetingType):
            raise TypeError("meeting_type must be a RemoteMeetingType or None")
        if self.kind is CatalogKind.MEETING:
            if self.week_start is None or self.meeting_type is None:
                raise ValueError("meeting collections require week_start and meeting_type")
        elif self.week_start is not None or self.meeting_type is not None:
            raise ValueError("week_start and meeting_type are only valid for meetings")
        if not isinstance(self.nodes, tuple) or not all(
            isinstance(node, CatalogNode) for node in self.nodes
        ):
            raise TypeError("nodes must be a tuple of CatalogNode objects")
        node_ids: set[str] = set()
        for node in _walk_catalog_nodes(self.nodes):
            if node.id in node_ids:
                raise ValueError(f"duplicate catalog node id: {node.id}")
            node_ids.add(node.id)

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "subtitle": self.subtitle,
            "thumbnailId": self.thumbnail_id,
            "weekStart": self.week_start.isoformat() if self.week_start else None,
            "meetingType": self.meeting_type.value if self.meeting_type else None,
            "nodes": [node.to_dict() for node in self.nodes],
        }


def _walk_catalog_nodes(nodes: tuple[CatalogNode, ...]):
    for node in nodes:
        yield node
        yield from _walk_catalog_nodes(node.children)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    catalog_revision: int
    collections: tuple[CatalogCollection, ...] = ()

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.catalog_revision, field_name="catalog_revision")
        if not isinstance(self.collections, tuple) or not all(
            isinstance(collection, CatalogCollection) for collection in self.collections
        ):
            raise TypeError("collections must be a tuple of CatalogCollection objects")
        collection_ids = [collection.id for collection in self.collections]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("catalog collection ids must be unique")

    def to_dict(self) -> JsonObject:
        return {
            "catalogRevision": self.catalog_revision,
            "collections": [collection.to_dict() for collection in self.collections],
        }


@dataclass(frozen=True, slots=True)
class ProjectionQueueItem:
    origin: ProjectionOrigin
    title: str
    media_kind: RemoteMediaKind
    thumbnail_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.origin, ProjectionOrigin):
            raise TypeError("origin must be a ProjectionOrigin")
        _require_string(self.title, field_name="title")
        if not isinstance(self.media_kind, RemoteMediaKind):
            raise TypeError("media_kind must be a RemoteMediaKind")
        if self.thumbnail_id is not None:
            _require_identifier(self.thumbnail_id, field_name="thumbnail_id")

    def to_dict(self) -> JsonObject:
        return {
            "origin": self.origin.to_dict(),
            "title": self.title,
            "mediaKind": self.media_kind.value,
            "thumbnailId": self.thumbnail_id,
        }


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    playback_revision: int
    state: PlaybackState
    playback_session_id: str | None = None
    origin: ProjectionOrigin | None = None
    title: str = ""
    media_kind: RemoteMediaKind | None = None
    queue: tuple[ProjectionQueueItem, ...] = ()
    current_index: int | None = None
    position_ms: int = 0
    duration_ms: int | None = None
    volume: int = 100
    capabilities: PlaybackCapabilities = PlaybackCapabilities()
    error: ProjectionError | None = None

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.playback_revision, field_name="playback_revision")
        if not isinstance(self.state, PlaybackState):
            raise TypeError("state must be a PlaybackState")
        if self.playback_session_id is not None:
            _require_identifier(self.playback_session_id, field_name="playback_session_id")
        if self.origin is not None and not isinstance(self.origin, ProjectionOrigin):
            raise TypeError("origin must be a ProjectionOrigin or None")
        _require_string(self.title, field_name="title", allow_empty=True)
        if self.media_kind is not None and not isinstance(self.media_kind, RemoteMediaKind):
            raise TypeError("media_kind must be a RemoteMediaKind or None")
        if not isinstance(self.queue, tuple) or not all(
            isinstance(item, ProjectionQueueItem) for item in self.queue
        ):
            raise TypeError("queue must be a tuple of ProjectionQueueItem objects")
        if self.current_index is not None:
            _require_nonnegative_int(self.current_index, field_name="current_index")
            if self.current_index >= len(self.queue):
                raise ValueError("current_index must identify an item in queue")
        elif self.queue:
            raise ValueError("a non-empty queue requires current_index")
        _require_nonnegative_int(self.position_ms, field_name="position_ms")
        _require_optional_nonnegative_int(self.duration_ms, field_name="duration_ms")
        if self.duration_ms is not None and self.position_ms > self.duration_ms:
            raise ValueError("position_ms must not exceed duration_ms")
        _require_nonnegative_int(self.volume, field_name="volume")
        if self.volume > 100:
            raise ValueError("volume must not exceed 100")
        if not isinstance(self.capabilities, PlaybackCapabilities):
            raise TypeError("capabilities must be PlaybackCapabilities")
        if self.error is not None and not isinstance(self.error, ProjectionError):
            raise TypeError("error must be a ProjectionError or None")
        if self.state is PlaybackState.IDLE:
            if self.playback_session_id is not None or self.origin is not None:
                raise ValueError("idle playback cannot have a session or origin")
            if self.queue or self.current_index is not None:
                raise ValueError("idle playback cannot have a queue")
        elif self.playback_session_id is None:
            raise ValueError("active playback requires playback_session_id")
        if self.state is PlaybackState.ERROR and self.error is None:
            raise ValueError("error playback state requires error details")
        if self.state is not PlaybackState.ERROR and self.error is not None:
            raise ValueError("playback error details require the error state")

    @classmethod
    def idle(cls, *, playback_revision: int = 0) -> ProjectionSnapshot:
        return cls(
            playback_revision=playback_revision,
            state=PlaybackState.IDLE,
        )

    def to_dict(self) -> JsonObject:
        return {
            "playbackRevision": self.playback_revision,
            "playbackSessionId": self.playback_session_id,
            "state": self.state.value,
            "origin": self.origin.to_dict() if self.origin is not None else None,
            "title": self.title,
            "mediaKind": self.media_kind.value if self.media_kind is not None else None,
            "queue": [item.to_dict() for item in self.queue],
            "currentIndex": self.current_index,
            "positionMs": self.position_ms,
            "durationMs": self.duration_ms,
            "volume": self.volume,
            "capabilities": self.capabilities.to_dict(),
            "error": self.error.to_dict() if self.error is not None else None,
        }


class CommandType(StrEnum):
    PLAY = "play"
    PAUSE = "pause"
    RESUME = "resume"
    SEEK = "seek"
    SET_VOLUME = "set_volume"
    PREVIOUS = "previous"
    NEXT = "next"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ProjectionCommand:
    command_id: str

    TYPE: ClassVar[CommandType]

    def __post_init__(self) -> None:
        _require_uuid(self.command_id, field_name="command_id")
        if not hasattr(type(self), "TYPE"):
            raise TypeError("ProjectionCommand is an abstract command contract")

    @property
    def type(self) -> CommandType:
        return self.TYPE

    def to_dict(self) -> JsonObject:
        return {"commandId": self.command_id, "type": self.type.value}


@dataclass(frozen=True, slots=True)
class PlayCommand(ProjectionCommand):
    origin: ProjectionOrigin
    catalog_revision: int
    start_paused: bool = False

    TYPE: ClassVar[CommandType] = CommandType.PLAY

    def __post_init__(self) -> None:
        super(PlayCommand, self).__post_init__()
        if not isinstance(self.origin, ProjectionOrigin):
            raise TypeError("origin must be a ProjectionOrigin")
        if self.origin.source is ProjectionSource.TEMPORARY:
            raise ValueError("temporary projections cannot be started from the remote catalog")
        _require_nonnegative_int(self.catalog_revision, field_name="catalog_revision")
        if not isinstance(self.start_paused, bool):
            raise TypeError("start_paused must be a boolean")

    def to_dict(self) -> JsonObject:
        payload = super(PlayCommand, self).to_dict()
        payload.update(
            {
                "origin": self.origin.to_dict(),
                "catalogRevision": self.catalog_revision,
                "startPaused": self.start_paused,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class SessionProjectionCommand(ProjectionCommand):
    playback_session_id: str

    def __post_init__(self) -> None:
        super(SessionProjectionCommand, self).__post_init__()
        _require_identifier(self.playback_session_id, field_name="playback_session_id")

    def to_dict(self) -> JsonObject:
        payload = super(SessionProjectionCommand, self).to_dict()
        payload["playbackSessionId"] = self.playback_session_id
        return payload


@dataclass(frozen=True, slots=True)
class PauseCommand(SessionProjectionCommand):
    TYPE: ClassVar[CommandType] = CommandType.PAUSE


@dataclass(frozen=True, slots=True)
class ResumeCommand(SessionProjectionCommand):
    TYPE: ClassVar[CommandType] = CommandType.RESUME


@dataclass(frozen=True, slots=True)
class SeekCommand(SessionProjectionCommand):
    position_ms: int

    TYPE: ClassVar[CommandType] = CommandType.SEEK

    def __post_init__(self) -> None:
        super(SeekCommand, self).__post_init__()
        _require_nonnegative_int(self.position_ms, field_name="position_ms")

    def to_dict(self) -> JsonObject:
        payload = super(SeekCommand, self).to_dict()
        payload["positionMs"] = self.position_ms
        return payload


@dataclass(frozen=True, slots=True)
class SetVolumeCommand(SessionProjectionCommand):
    volume: int

    TYPE: ClassVar[CommandType] = CommandType.SET_VOLUME

    def __post_init__(self) -> None:
        super(SetVolumeCommand, self).__post_init__()
        _require_nonnegative_int(self.volume, field_name="volume")
        if self.volume > 100:
            raise ValueError("volume must not exceed 100")

    def to_dict(self) -> JsonObject:
        payload = super(SetVolumeCommand, self).to_dict()
        payload["volume"] = self.volume
        return payload


@dataclass(frozen=True, slots=True)
class PreviousCommand(SessionProjectionCommand):
    TYPE: ClassVar[CommandType] = CommandType.PREVIOUS


@dataclass(frozen=True, slots=True)
class NextCommand(SessionProjectionCommand):
    TYPE: ClassVar[CommandType] = CommandType.NEXT


@dataclass(frozen=True, slots=True)
class StopCommand(ProjectionCommand):
    TYPE: ClassVar[CommandType] = CommandType.STOP


RemoteCommand: TypeAlias = (
    PlayCommand
    | PauseCommand
    | ResumeCommand
    | SeekCommand
    | SetVolumeCommand
    | PreviousCommand
    | NextCommand
    | StopCommand
)


class CommandErrorCode(StrEnum):
    CATALOG_STALE = "catalog_stale"
    PLAYBACK_STALE = "playback_stale"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    INVALID = "invalid"
    COMMAND_ID_CONFLICT = "command_id_conflict"
    COMMAND_IN_PROGRESS = "command_in_progress"


@dataclass(frozen=True, slots=True)
class CommandError:
    code: CommandErrorCode
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, CommandErrorCode):
            raise TypeError("code must be a CommandErrorCode")
        _require_string(
            self.message,
            field_name="message",
            maximum=MAX_ERROR_TEXT_LENGTH,
        )
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    ok: bool
    catalog_revision: int
    playback_revision: int
    error: CommandError | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.command_id, field_name="command_id")
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a boolean")
        _require_nonnegative_int(self.catalog_revision, field_name="catalog_revision")
        _require_nonnegative_int(self.playback_revision, field_name="playback_revision")
        if self.error is not None and not isinstance(self.error, CommandError):
            raise TypeError("error must be a CommandError or None")
        if self.ok == (self.error is not None):
            raise ValueError("successful results cannot have errors; failed results require one")

    @classmethod
    def success(
        cls,
        command_id: str,
        *,
        catalog_revision: int,
        playback_revision: int,
    ) -> CommandResult:
        return cls(
            command_id=command_id,
            ok=True,
            catalog_revision=catalog_revision,
            playback_revision=playback_revision,
        )

    @classmethod
    def failure(
        cls,
        command_id: str,
        *,
        catalog_revision: int,
        playback_revision: int,
        error: CommandError,
    ) -> CommandResult:
        return cls(
            command_id=command_id,
            ok=False,
            catalog_revision=catalog_revision,
            playback_revision=playback_revision,
            error=error,
        )

    def to_dict(self) -> JsonObject:
        return {
            "commandId": self.command_id,
            "ok": self.ok,
            "catalogRevision": self.catalog_revision,
            "playbackRevision": self.playback_revision,
            "error": self.error.to_dict() if self.error is not None else None,
        }


def parse_projection_command(payload: Mapping[str, object]) -> RemoteCommand:
    """Parse and validate the strict JSON command envelope."""

    if not isinstance(payload, Mapping):
        raise TypeError("command payload must be an object")
    command_id = _payload_string(payload, "commandId")
    try:
        command_type = CommandType(payload.get("type"))
    except (TypeError, ValueError) as exc:
        raise ValueError("command type is invalid") from exc

    if command_type is CommandType.PLAY:
        _reject_unknown_fields(
            payload,
            {"commandId", "type", "origin", "catalogRevision", "startPaused"},
        )
        origin_payload = payload.get("origin")
        if not isinstance(origin_payload, Mapping):
            raise TypeError("origin must be an object")
        return PlayCommand(
            command_id=command_id,
            origin=ProjectionOrigin.from_dict(origin_payload),
            catalog_revision=_payload_int(payload, "catalogRevision"),
            start_paused=_payload_bool(payload, "startPaused", default=False),
        )
    if command_type is CommandType.STOP:
        _reject_unknown_fields(payload, {"commandId", "type"})
        return StopCommand(command_id=command_id)

    allowed_fields = {"commandId", "type", "playbackSessionId"}
    if command_type is CommandType.SEEK:
        allowed_fields.add("positionMs")
    elif command_type is CommandType.SET_VOLUME:
        allowed_fields.add("volume")
    _reject_unknown_fields(payload, allowed_fields)
    playback_session_id = _payload_string(payload, "playbackSessionId")
    if command_type is CommandType.PAUSE:
        return PauseCommand(command_id, playback_session_id)
    if command_type is CommandType.RESUME:
        return ResumeCommand(command_id, playback_session_id)
    if command_type is CommandType.SEEK:
        return SeekCommand(command_id, playback_session_id, _payload_int(payload, "positionMs"))
    if command_type is CommandType.SET_VOLUME:
        return SetVolumeCommand(command_id, playback_session_id, _payload_int(payload, "volume"))
    if command_type is CommandType.PREVIOUS:
        return PreviousCommand(command_id, playback_session_id)
    return NextCommand(command_id, playback_session_id)


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _payload_bool(payload: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _reject_unknown_fields(payload: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ValueError(f"unknown command field: {unknown[0]}")
