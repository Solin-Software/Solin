"""Read-only remote catalog projection and private play resolution.

This module is the only boundary that knows both persisted media records and
the public remote-control contracts.  Catalog DTOs contain display metadata
and stable IDs only.  Filesystem paths, URLs, and media references remain in
the desktop process and are returned only by :meth:`RemoteCatalog.resolve_play`.
"""

from __future__ import annotations

from _thread import RLock as ReentrantLock
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias
import uuid

from solin.core.media.duration import effective_duration_ticks
from solin.core.media.formats import MediaKind, media_kind_from_mime, media_kind_from_path
from solin.core.meetings.colors import accent_from_hue
from solin.core.meetings.media_nodes import meeting_media_from_ref
from solin.core.meetings.models import MeetingMedia
from solin.core.meetings.tree_store import MeetingTreeSnapshot, MeetingTreeStore

from .contracts import (
    CatalogCollection,
    CatalogKind,
    CatalogNode,
    CatalogNodeKind,
    CatalogSnapshot,
    PlayCommand,
    ProjectionOrigin,
    ProjectionSource,
    RemoteMeetingType,
    RemoteMediaKind,
)
from .sanitization import public_reason, public_title
from .thumbnails import PrivateMediaThumbnailSource, private_media_thumbnail_source


_MARKER_POSITION_FALLBACK = 1_000_000_000
_MAX_IDENTIFIER_LENGTH = 512
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._~-]+\Z")


class _PlaylistSource(Protocol):
    def load(self) -> list[dict]: ...


MeetingSource: TypeAlias = (
    MeetingTreeStore
    | MeetingTreeSnapshot
    | Iterable[MeetingTreeSnapshot]
    | Callable[[], Iterable[MeetingTreeSnapshot]]
)


@dataclass(frozen=True, slots=True)
class MediaAvailability:
    """Public availability state calculated from a private persisted record."""

    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")


AvailabilityResolver: TypeAlias = Callable[[CatalogKind, str, Mapping[str, Any]], MediaAvailability]
ThumbnailIdResolver: TypeAlias = Callable[[CatalogKind, str, Mapping[str, Any]], str | None]
MeetingGroupTitleResolver: TypeAlias = Callable[[Mapping[str, Any]], str]


class CatalogResolutionCode(StrEnum):
    STALE_CATALOG = "stale_catalog"
    SOURCE_MISMATCH = "source_mismatch"
    COLLECTION_NOT_FOUND = "collection_not_found"
    NODE_NOT_FOUND = "node_not_found"
    NODE_NOT_PLAYABLE = "node_not_playable"
    INVALID_CATALOG = "invalid_catalog"


class CatalogResolutionError(LookupError):
    """Typed failure safe to map to a remote command error."""

    def __init__(self, code: CatalogResolutionCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedPlaylistPlay:
    """Authoritative playlist queue retained inside the desktop boundary."""

    collection_id: str
    items: tuple[dict[str, Any], ...]
    current_index: int
    start_paused: bool

    @property
    def current_item(self) -> dict[str, Any]:
        return self.items[self.current_index]


@dataclass(frozen=True, slots=True)
class ResolvedMeetingPlay:
    """Authoritative meeting media retained inside the desktop boundary."""

    collection_id: str
    media: MeetingMedia
    start_paused: bool


ResolvedPlay: TypeAlias = ResolvedPlaylistPlay | ResolvedMeetingPlay


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    """Side-effect-free candidate containing public and private catalog data.

    A build has no wire revision until the application commits it. This keeps
    superseded background work from advancing the published catalog epoch.
    """

    collections: tuple[CatalogCollection, ...]
    playlists: tuple[dict[str, Any], ...]
    linked_playlists: tuple[dict[str, Any], ...]
    meetings: tuple[MeetingTreeSnapshot, ...]
    playable_origins: frozenset[tuple[ProjectionSource, str, str]]
    fingerprint: str


class RemoteCatalog:
    """Project persisted catalog data and resolve play commands by stable IDs.

    The adapter is framework-independent and never imports widgets or QML.  A
    snapshot revision is incremented when public metadata *or private playback
    references* change, preventing a command based on an older view from
    silently playing different content.
    """

    def __init__(
        self,
        playlist_source: _PlaylistSource | Callable[[], Sequence[dict]],
        meeting_source: MeetingSource,
        *,
        linked_playlist_source: _PlaylistSource | Callable[[], Sequence[dict]] | None = None,
        availability_resolver: AvailabilityResolver | None = None,
        thumbnail_id_resolver: ThumbnailIdResolver | None = None,
        meeting_group_title_resolver: MeetingGroupTitleResolver | None = None,
        publication_lock: ReentrantLock | None = None,
    ) -> None:
        self._playlist_source = playlist_source
        self._linked_playlist_source = linked_playlist_source
        self._meeting_source = (
            meeting_source
            if isinstance(meeting_source, (MeetingTreeStore, MeetingTreeSnapshot))
            or callable(meeting_source)
            else tuple(meeting_source)
        )
        self._availability_resolver = availability_resolver
        self._thumbnail_id_resolver = thumbnail_id_resolver
        self._meeting_group_title_resolver = meeting_group_title_resolver
        self._lock = publication_lock or ReentrantLock()
        self._revision = 0
        self._published: CatalogBuild | None = None

    def snapshot(self) -> CatalogSnapshot:
        """Build and publish a snapshot synchronously.

        Application code that builds on a worker must use :meth:`build` and
        publish only the newest accepted candidate with :meth:`publish`.
        """

        return self.publish(self.build())

    def build(self) -> CatalogBuild:
        """Build a complete candidate without changing published state."""

        return self._build_view()

    def publish(
        self,
        build: CatalogBuild,
        *,
        revision: int | None = None,
    ) -> CatalogSnapshot:
        """Atomically install one candidate and its authoritative wire revision."""

        if not isinstance(build, CatalogBuild):
            raise TypeError("build must be a CatalogBuild")
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
        ):
            raise ValueError("revision must be a non-negative integer")

        with self._lock:
            changed = self._published is None or build.fingerprint != self._published.fingerprint
            next_revision = (
                (self._revision + 1 if changed else self._revision)
                if revision is None
                else revision
            )
            if next_revision < self._revision:
                raise ValueError("published catalog revision cannot regress")
            if changed and self._published is not None and next_revision == self._revision:
                raise ValueError("changed catalog content requires a newer revision")
            self._published = build
            self._revision = next_revision
            return CatalogSnapshot(next_revision, build.collections)

    def resolve_play(self, command: PlayCommand) -> ResolvedPlay:
        """Resolve a public play command to private authoritative media data."""

        if not isinstance(command, PlayCommand):
            raise TypeError("command must be a PlayCommand")
        with self._lock:
            view = self._published_view()
            if command.catalog_revision != self._revision:
                raise CatalogResolutionError(
                    CatalogResolutionCode.STALE_CATALOG,
                    "The catalog changed. Refresh it before playing media.",
                )

            origin = command.origin
            if origin.source in (ProjectionSource.PLAYLIST, ProjectionSource.LINKED_FOLDER):
                return self._resolve_playlist(command, view)
            if origin.source is ProjectionSource.MEETING:
                return self._resolve_meeting(command, view)
            raise CatalogResolutionError(
                CatalogResolutionCode.SOURCE_MISMATCH,
                "This catalog source is not available.",
            )

    def resolve_local_media(
        self,
        origin: ProjectionOrigin,
    ) -> PrivateMediaThumbnailSource | None:
        """Resolve a public origin to private stored media for thumbnailing."""

        if not isinstance(origin, ProjectionOrigin):
            raise TypeError("origin must be a ProjectionOrigin")
        if origin.collection_id is None or origin.node_id is None:
            return None
        with self._lock:
            view = self._published
            if view is None:
                return None
            public_key = (origin.source, origin.collection_id, origin.node_id)
            if public_key not in view.playable_origins:
                return None
            if origin.source in (
                ProjectionSource.PLAYLIST,
                ProjectionSource.LINKED_FOLDER,
            ):
                candidates = (
                    view.linked_playlists
                    if origin.source is ProjectionSource.LINKED_FOLDER
                    else view.playlists
                )
                collection = next(
                    (
                        value
                        for value in candidates
                        if str(value.get("id") or "") == origin.collection_id
                    ),
                    None,
                )
                if collection is None:
                    return None
                raw = next(
                    (
                        item
                        for item in _object_list(collection.get("items"), "playlist items")
                        if str(item.get("id") or "") == origin.node_id
                    ),
                    None,
                )
            elif origin.source is ProjectionSource.MEETING:
                meeting = next(
                    (value for value in view.meetings if value.tree_key == origin.collection_id),
                    None,
                )
                raw = _find_node(meeting.nodes, origin.node_id) if meeting else None
            else:
                return None
            return private_media_thumbnail_source(raw) if raw is not None else None

    def resolve_collection_thumbnail(
        self,
        source: ProjectionSource,
        collection_id: str,
    ) -> bytes | None:
        """Resolve a public collection to its private persisted cover bytes."""

        if source is not ProjectionSource.MEETING:
            return None
        with self._lock:
            view = self._published
            if view is None:
                return None
            collection = next(
                (
                    value
                    for value in view.collections
                    if value.id == collection_id
                    and value.kind is CatalogKind.MEETING
                    and value.thumbnail_id is not None
                ),
                None,
            )
            meeting = next(
                (value for value in view.meetings if value.tree_key == collection_id),
                None,
            )
            if collection is None or meeting is None:
                return None
            cover = meeting.overview.cover_bytes
            if not cover or collection.thumbnail_id != _meeting_cover_thumbnail_id(cover):
                return None
            return bytes(cover)

    def _published_view(self) -> CatalogBuild:
        view = self._published
        if view is None:
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "The catalog has not been published yet.",
            )
        return view

    def _build_view(self) -> CatalogBuild:
        playlists = tuple(deepcopy(self._load_playlists()))
        linked_playlists = tuple(deepcopy(self._load_linked_playlists()))
        meetings = tuple(self._load_meetings())
        collections = tuple(
            [self._playlist_collection(playlist, CatalogKind.PLAYLIST) for playlist in playlists]
            + [
                self._playlist_collection(playlist, CatalogKind.LINKED_FOLDER)
                for playlist in linked_playlists
            ]
            + [self._meeting_collection(snapshot) for snapshot in meetings]
        )
        fingerprint_payload = {
            "public": [collection.to_dict() for collection in collections],
            "private": {
                "playlists": playlists,
                "linkedPlaylists": linked_playlists,
                "meetings": [
                    {
                        "treeKey": snapshot.tree_key,
                        "revision": snapshot.revision,
                        "nodes": snapshot.nodes,
                    }
                    for snapshot in meetings
                ],
            },
        }
        encoded = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_fingerprint_default,
        ).encode("utf-8")
        playable_origins = frozenset(
            (
                _projection_source(collection.kind),
                collection.id,
                node.id,
            )
            for collection in collections
            for node in _walk_catalog_nodes(collection.nodes)
            if node.kind is CatalogNodeKind.MEDIA and node.available
        )
        return CatalogBuild(
            collections,
            playlists,
            linked_playlists,
            meetings,
            playable_origins,
            hashlib.sha256(encoded).hexdigest(),
        )

    def _load_playlists(self) -> Sequence[dict]:
        return self._load_playlist_source(self._playlist_source)

    def _load_linked_playlists(self) -> Sequence[dict]:
        if self._linked_playlist_source is None:
            return ()
        return self._load_playlist_source(self._linked_playlist_source)

    @staticmethod
    def _load_playlist_source(
        source: _PlaylistSource | Callable[[], Sequence[dict]],
    ) -> Sequence[dict]:
        loaded = source() if callable(source) else source.load()
        if not isinstance(loaded, Sequence) or isinstance(loaded, (str, bytes)):
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "Playlist catalog data is invalid.",
            )
        if not all(isinstance(playlist, dict) for playlist in loaded):
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "Playlist catalog entries must be objects.",
            )
        return loaded

    def _load_meetings(self) -> tuple[MeetingTreeSnapshot, ...]:
        source = self._meeting_source
        if isinstance(source, MeetingTreeSnapshot):
            snapshots = (source,)
        elif isinstance(source, MeetingTreeStore):
            all_data = source.load_all()
            trees = all_data.get("trees", {})
            if not isinstance(trees, dict):
                raise CatalogResolutionError(
                    CatalogResolutionCode.INVALID_CATALOG,
                    "Meeting catalog data is invalid.",
                )
            snapshots = tuple(
                snapshot
                for tree_key in trees
                if isinstance(tree_key, str) and (snapshot := source.snapshot(tree_key)) is not None
            )
        else:
            loaded = source() if callable(source) else source
            snapshots = tuple(loaded)
        if not all(isinstance(snapshot, MeetingTreeSnapshot) for snapshot in snapshots):
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "Meeting catalog entries must be tree snapshots.",
            )
        return tuple(
            _with_unique_meeting_node_ids(snapshot)
            for snapshot in sorted(snapshots, key=_meeting_sort_key)
        )

    def _playlist_collection(
        self,
        playlist: dict[str, Any],
        kind: CatalogKind,
    ) -> CatalogCollection:
        collection_id = _public_identifier(playlist.get("id"), "playlist id")
        title = public_title(playlist.get("name"), collection_id)
        items = _object_list(playlist.get("items"), "playlist items")
        sections = _object_list(playlist.get("sections"), "playlist sections")
        markers = _object_list(playlist.get("markers"), "playlist markers")
        nodes = self._playlist_nodes(kind, collection_id, items, sections, markers)
        return CatalogCollection(
            id=collection_id,
            kind=kind,
            title=title,
            nodes=nodes,
        )

    def _playlist_nodes(
        self,
        kind: CatalogKind,
        collection_id: str,
        items: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        markers: list[dict[str, Any]],
    ) -> tuple[CatalogNode, ...]:
        section_pairs = [
            (_public_identifier(section.get("id"), "section id"), section) for section in sections
        ]
        sections_by_id = dict(section_pairs)
        top_sections = [
            section
            for section_id, section in section_pairs
            if not _optional_identifier(section.get("parent_id"))
        ]
        subsections_by_parent: dict[str, list[dict[str, Any]]] = {}
        for _section_id, section in section_pairs:
            parent_id = _optional_identifier(section.get("parent_id"))
            if parent_id:
                subsections_by_parent.setdefault(parent_id, []).append(section)

        markers_by_subsection: dict[str, list[dict[str, Any]]] = {}
        for marker in markers:
            subsection_id = _optional_identifier(marker.get("subsection_id"))
            if subsection_id is None:
                continue
            subsection = sections_by_id.get(subsection_id)
            if not subsection or not _optional_identifier(subsection.get("parent_id")):
                continue
            markers_by_subsection.setdefault(subsection_id, []).append(marker)

        first_index: dict[str, int] = {}
        for index, item in enumerate(items):
            section_id = _optional_identifier(item.get("section_id"))
            if not section_id:
                continue
            first_index.setdefault(section_id, index)
            section = sections_by_id.get(section_id)
            parent_id = _optional_identifier(section.get("parent_id")) if section else None
            if parent_id:
                first_index.setdefault(parent_id, index)

        ordered: list[tuple[tuple[float, int, int, int], str, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if not _optional_identifier(item.get("section_id")):
                ordered.append((_media_order_key(index), "media", item))
        for fallback, section in enumerate(top_sections):
            ordered.append(
                (
                    _section_order_key(
                        section,
                        fallback,
                        len(items),
                        first_index,
                    ),
                    "section",
                    section,
                )
            )
        ordered.sort(key=lambda entry: entry[0])

        return tuple(
            self._media_node(kind, collection_id, raw)
            if node_type == "media"
            else self._playlist_section_node(
                collection_id,
                raw,
                items,
                subsections_by_parent,
                markers_by_subsection,
                kind=kind,
                is_subsection=False,
            )
            for _, node_type, raw in ordered
        )

    def _playlist_section_node(
        self,
        collection_id: str,
        section: dict[str, Any],
        items: list[dict[str, Any]],
        subsections_by_parent: dict[str, list[dict[str, Any]]],
        markers_by_subsection: dict[str, list[dict[str, Any]]],
        *,
        kind: CatalogKind,
        is_subsection: bool,
    ) -> CatalogNode:
        section_id = _public_identifier(section.get("id"), "section id")
        if is_subsection:
            children = self._playlist_subsection_children(
                collection_id,
                section_id,
                items,
                markers_by_subsection,
                kind=kind,
            )
        else:
            subsections = subsections_by_parent.get(section_id, [])
            first_index = _first_index_by_section(items)
            child_entries: list[tuple[tuple[float, int, int, int], str, dict[str, Any]]] = []
            for index, item in enumerate(items):
                if _optional_identifier(item.get("section_id")) == section_id:
                    child_entries.append((_media_order_key(index), "media", item))
            for fallback, subsection in enumerate(subsections):
                child_entries.append(
                    (
                        _section_order_key(
                            subsection,
                            fallback,
                            len(items),
                            first_index,
                        ),
                        "subsection",
                        subsection,
                    )
                )
            child_entries.sort(key=lambda entry: entry[0])
            children = tuple(
                self._media_node(kind, collection_id, raw)
                if node_type == "media"
                else self._playlist_section_node(
                    collection_id,
                    raw,
                    items,
                    subsections_by_parent,
                    markers_by_subsection,
                    kind=kind,
                    is_subsection=True,
                )
                for _, node_type, raw in child_entries
            )

        default_hue = 145 if is_subsection else 215
        hue = _valid_int(section.get("color_hue"), default_hue)
        return CatalogNode(
            id=section_id,
            kind=(CatalogNodeKind.SUBSECTION if is_subsection else CatalogNodeKind.SECTION),
            title=public_title(section.get("name"), section_id),
            color=accent_from_hue(hue % 360),
            collapsed=_valid_bool(section.get("collapsed"), False),
            children=children,
        )

    def _playlist_subsection_children(
        self,
        collection_id: str,
        section_id: str,
        items: list[dict[str, Any]],
        markers_by_subsection: dict[str, list[dict[str, Any]]],
        *,
        kind: CatalogKind,
    ) -> tuple[CatalogNode, ...]:
        ordered: list[tuple[float, int, int, int, str, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if _optional_identifier(item.get("section_id")) == section_id:
                ordered.append((float(index), 1, 0, index, "media", item))
        for fallback, marker in enumerate(markers_by_subsection.get(section_id, [])):
            position = _valid_int(
                marker.get("position"),
                _MARKER_POSITION_FALLBACK + fallback,
            )
            slot_order = _valid_int(marker.get("slot_order"), fallback)
            ordered.append((float(position), 0, slot_order, fallback, "marker", marker))
        ordered.sort(key=lambda entry: entry[:4])
        return tuple(
            self._media_node(kind, collection_id, raw)
            if node_type == "media"
            else CatalogNode(
                id=_public_identifier(raw.get("id"), "marker id"),
                kind=CatalogNodeKind.MARKER,
                title=public_title(
                    raw.get("text"),
                    _public_identifier(raw.get("id"), "marker id"),
                ),
            )
            for _, _, _, _, node_type, raw in ordered
        )

    def _meeting_collection(
        self,
        snapshot: MeetingTreeSnapshot,
    ) -> CatalogCollection:
        collection_id = _public_identifier(snapshot.tree_key, "meeting tree id")
        nodes = tuple(
            self._meeting_node(collection_id, node)
            for node in _object_list(snapshot.nodes, "meeting tree nodes")
        )
        title = public_title(snapshot.overview.title, snapshot.pub_type.upper())
        meeting_type = {
            "mwb": RemoteMeetingType.MIDWEEK,
            "wt": RemoteMeetingType.WEEKEND,
            "memorial": RemoteMeetingType.MEMORIAL,
        }.get(snapshot.pub_type, RemoteMeetingType.OTHER)
        return CatalogCollection(
            id=collection_id,
            kind=CatalogKind.MEETING,
            title=title,
            thumbnail_id=_meeting_cover_thumbnail_id(snapshot.overview.cover_bytes),
            week_start=snapshot.monday,
            meeting_type=meeting_type,
            nodes=nodes,
        )

    def _meeting_node(
        self,
        collection_id: str,
        raw: dict[str, Any],
    ) -> CatalogNode:
        node_id = _public_identifier(raw.get("id"), "meeting node id")
        raw_kind = str(raw.get("type") or "").lower()
        kinds = {
            "section": CatalogNodeKind.SECTION,
            "subsection": CatalogNodeKind.SUBSECTION,
            "marker": CatalogNodeKind.MARKER,
            "media": CatalogNodeKind.MEDIA,
        }
        try:
            kind = kinds[raw_kind]
        except KeyError as exc:
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "Meeting tree contains an unsupported node type.",
            ) from exc
        if kind is CatalogNodeKind.MEDIA:
            return self._media_node(CatalogKind.MEETING, collection_id, raw)

        children = tuple(
            self._meeting_node(collection_id, child)
            for child in _object_list(raw.get("children"), "meeting node children")
        )
        hue = _valid_int(raw.get("color_hue"), 215)
        title = raw.get("text") if kind is CatalogNodeKind.MARKER else raw.get("title")
        if (
            kind in (CatalogNodeKind.SECTION, CatalogNodeKind.SUBSECTION)
            and self._meeting_group_title_resolver is not None
        ):
            title = self._meeting_group_title_resolver(raw)
        return CatalogNode(
            id=node_id,
            kind=kind,
            title=public_title(title, node_id),
            children=children,
            color=(accent_from_hue(hue % 360) if kind is not CatalogNodeKind.MARKER else None),
            collapsed=(
                _valid_bool(raw.get("collapsed"), False)
                if kind in (CatalogNodeKind.SECTION, CatalogNodeKind.SUBSECTION)
                else False
            ),
        )

    def _media_node(
        self,
        catalog_kind: CatalogKind,
        collection_id: str,
        raw: dict[str, Any],
    ) -> CatalogNode:
        node_id = _public_identifier(raw.get("id"), "media id")
        availability = self._availability(catalog_kind, collection_id, raw)
        reason = None if availability.available else public_reason(availability.reason)
        return CatalogNode(
            id=node_id,
            kind=CatalogNodeKind.MEDIA,
            title=public_title(raw.get("title"), node_id),
            media_kind=_media_kind(raw),
            duration_ms=_duration_ms(raw),
            thumbnail_id=self._thumbnail_id(catalog_kind, collection_id, raw),
            available=availability.available,
            unavailable_reason=reason,
        )

    def _availability(
        self,
        kind: CatalogKind,
        collection_id: str,
        raw: dict[str, Any],
    ) -> MediaAvailability:
        if self._availability_resolver is None:
            return MediaAvailability(True)
        result = self._availability_resolver(
            kind,
            collection_id,
            MappingProxyType(deepcopy(raw)),
        )
        if not isinstance(result, MediaAvailability):
            raise TypeError("availability_resolver must return MediaAvailability")
        return result

    def _thumbnail_id(
        self,
        kind: CatalogKind,
        collection_id: str,
        raw: dict[str, Any],
    ) -> str | None:
        if self._thumbnail_id_resolver is None:
            return None
        thumbnail_id = self._thumbnail_id_resolver(
            kind,
            collection_id,
            MappingProxyType(deepcopy(raw)),
        )
        if thumbnail_id is None:
            return None
        if (
            not isinstance(thumbnail_id, str)
            or not thumbnail_id
            or len(thumbnail_id) > _MAX_IDENTIFIER_LENGTH
            or _OPAQUE_ID.fullmatch(thumbnail_id) is None
            or thumbnail_id in {".", ".."}
        ):
            raise CatalogResolutionError(
                CatalogResolutionCode.INVALID_CATALOG,
                "Thumbnail IDs must be opaque URL-safe tokens.",
            )
        return thumbnail_id

    def _resolve_playlist(
        self,
        command: PlayCommand,
        view: CatalogBuild,
    ) -> ResolvedPlaylistPlay:
        collection_id = command.origin.collection_id or ""
        node_id = command.origin.node_id or ""
        source = command.origin.source
        candidates = (
            view.linked_playlists if source is ProjectionSource.LINKED_FOLDER else view.playlists
        )
        playlist = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("id") or "") == collection_id
            ),
            None,
        )
        if playlist is None:
            raise CatalogResolutionError(
                CatalogResolutionCode.COLLECTION_NOT_FOUND,
                "The playlist no longer exists.",
            )
        items = _object_list(playlist.get("items"), "playlist items")
        selected_index = next(
            (index for index, item in enumerate(items) if str(item.get("id") or "") == node_id),
            None,
        )
        if selected_index is None:
            raise CatalogResolutionError(
                CatalogResolutionCode.NODE_NOT_PLAYABLE,
                "The selected playlist node is not playable.",
            )
        if (
            source,
            collection_id,
            node_id,
        ) not in view.playable_origins:
            raise CatalogResolutionError(
                CatalogResolutionCode.NODE_NOT_PLAYABLE,
                "The selected playlist media is unavailable.",
            )
        resolved_items: list[dict[str, Any]] = []
        for item in items:
            resolved = deepcopy(item)
            resolved["origin_kind"] = source.value
            resolved["origin_container_id"] = collection_id
            resolved["origin_item_id"] = _public_identifier(item.get("id"), "media id")
            resolved_items.append(resolved)
        return ResolvedPlaylistPlay(
            collection_id,
            tuple(resolved_items),
            selected_index,
            command.start_paused,
        )

    def _resolve_meeting(
        self,
        command: PlayCommand,
        view: CatalogBuild,
    ) -> ResolvedMeetingPlay:
        collection_id = command.origin.collection_id or ""
        node_id = command.origin.node_id or ""
        snapshot = next(
            (meeting for meeting in view.meetings if meeting.tree_key == collection_id),
            None,
        )
        if snapshot is None:
            raise CatalogResolutionError(
                CatalogResolutionCode.COLLECTION_NOT_FOUND,
                "The meeting no longer exists.",
            )
        raw = _find_node(snapshot.nodes, node_id)
        if raw is None:
            raise CatalogResolutionError(
                CatalogResolutionCode.NODE_NOT_FOUND,
                "The meeting node no longer exists.",
            )
        if raw.get("type") != "media" or not isinstance(raw.get("media_ref"), dict):
            raise CatalogResolutionError(
                CatalogResolutionCode.NODE_NOT_PLAYABLE,
                "The selected meeting node is not playable.",
            )
        if (
            ProjectionSource.MEETING,
            collection_id,
            node_id,
        ) not in view.playable_origins:
            raise CatalogResolutionError(
                CatalogResolutionCode.NODE_NOT_PLAYABLE,
                "The selected meeting media is unavailable.",
            )
        media = meeting_media_from_ref(
            deepcopy(raw["media_ref"]),
            image_framing=deepcopy(raw.get("image_framing")),
            start_trim_ticks=_valid_int(raw.get("start_trim_ticks"), 0),
            end_trim_ticks=_valid_int(raw.get("end_trim_ticks"), 0),
            base_duration_ticks=_valid_int(raw.get("base_duration_ticks"), 0),
            origin_kind=ProjectionSource.MEETING.value,
            origin_container_id=collection_id,
            origin_item_id=node_id,
        )
        return ResolvedMeetingPlay(collection_id, media, command.start_paused)


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CatalogResolutionError(
            CatalogResolutionCode.INVALID_CATALOG,
            f"{label.capitalize()} are invalid.",
        )
    return value


def _public_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CatalogResolutionError(
            CatalogResolutionCode.INVALID_CATALOG,
            f"{label.capitalize()} must be a string.",
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_IDENTIFIER_LENGTH
        or "/" in normalized
        or "\\" in normalized
        or "://" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise CatalogResolutionError(
            CatalogResolutionCode.INVALID_CATALOG,
            f"{label.capitalize()} is not a safe public identifier.",
        )
    return normalized


def _optional_identifier(value: object) -> str | None:
    if value in (None, ""):
        return None
    return _public_identifier(value, "reference id")


def _valid_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _valid_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _media_order_key(index: int) -> tuple[float, int, int, int]:
    return (float(index), 1, 0, index)


def _section_order_key(
    section: dict[str, Any],
    fallback: int,
    item_count: int,
    first_index: dict[str, int],
) -> tuple[float, int, int, int]:
    section_id = _public_identifier(section.get("id"), "section id")
    position = _valid_int(
        section.get("position"),
        first_index.get(section_id, item_count + fallback + 1),
    )
    slot_order = _valid_int(section.get("slot_order"), fallback)
    return (float(position), 0, slot_order, fallback)


def _first_index_by_section(items: list[dict[str, Any]]) -> dict[str, int]:
    first_index: dict[str, int] = {}
    for index, item in enumerate(items):
        section_id = _optional_identifier(item.get("section_id"))
        if section_id:
            first_index.setdefault(section_id, index)
    return first_index


def _duration_ms(raw: Mapping[str, Any]) -> int | None:
    base = _valid_int(raw.get("base_duration_ticks"), 0)
    if base <= 0:
        return None
    ticks = effective_duration_ticks(
        base,
        _valid_int(raw.get("start_trim_ticks"), 0),
        _valid_int(raw.get("end_trim_ticks"), 0),
    )
    return ticks // 10_000


def _media_kind(raw: Mapping[str, Any]) -> RemoteMediaKind:
    raw_type = str(raw.get("media_type") or raw.get("type") or "").casefold()
    known = {kind.value: kind for kind in RemoteMediaKind}
    if raw_type in known:
        return known[raw_type]
    if raw_type in {"pdf", "presentation", "document"}:
        return RemoteMediaKind.DOCUMENT

    ref = raw.get("media_ref")
    media_ref = ref if isinstance(ref, Mapping) else {}
    mime_kind = media_kind_from_mime(str(media_ref.get("mime_type") or ""))
    if mime_kind is not MediaKind.UNKNOWN:
        return RemoteMediaKind(mime_kind.value)
    private_location = str(raw.get("url") or media_ref.get("file_path") or "")
    path_kind = media_kind_from_path(private_location)
    if path_kind is not MediaKind.UNKNOWN:
        return RemoteMediaKind(path_kind.value)
    return RemoteMediaKind.UNKNOWN


def _find_node(nodes: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            return node
        children = node.get("children")
        if isinstance(children, list):
            found = _find_node(children, node_id)
            if found is not None:
                return found
    return None


def _with_unique_meeting_node_ids(snapshot: MeetingTreeSnapshot) -> MeetingTreeSnapshot:
    """Return a remote-only tree whose node IDs are unique and deterministic.

    Persisted meeting trees predate the remote catalog's global node-ID
    invariant. Repeated media can therefore legitimately carry the same stable
    source ID. Keep the first occurrence unchanged for compatibility and assign
    later occurrences a path-derived UUID. The persisted tree is never mutated.
    """

    nodes = deepcopy(snapshot.nodes)
    seen: set[str] = set()

    def visit(values: list[dict[str, Any]], path: tuple[int, ...]) -> None:
        for index, node in enumerate(values):
            node_path = (*path, index)
            node_id = node.get("id")
            if isinstance(node_id, str) and node_id:
                public_id = node_id
                attempt = 0
                while public_id in seen:
                    attempt += 1
                    public_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            (
                                "solin:remote-meeting-node:"
                                f"{snapshot.tree_key}:{node_id}:"
                                f"{'.'.join(map(str, node_path))}:{attempt}"
                            ),
                        )
                    )
                if public_id != node_id:
                    node["id"] = public_id
                seen.add(public_id)

            children = node.get("children")
            if isinstance(children, list):
                visit(children, node_path)

    visit(nodes, ())
    return replace(snapshot, nodes=nodes)


def _walk_catalog_nodes(nodes: tuple[CatalogNode, ...]) -> Iterable[CatalogNode]:
    for node in nodes:
        yield node
        yield from _walk_catalog_nodes(node.children)


def _projection_source(kind: CatalogKind) -> ProjectionSource:
    if kind is CatalogKind.PLAYLIST:
        return ProjectionSource.PLAYLIST
    if kind is CatalogKind.LINKED_FOLDER:
        return ProjectionSource.LINKED_FOLDER
    return ProjectionSource.MEETING


def _meeting_cover_thumbnail_id(cover: bytes | None) -> str | None:
    if not cover:
        return None
    return f"cover-{hashlib.sha256(cover).hexdigest()[:24]}"


def _meeting_sort_key(snapshot: MeetingTreeSnapshot) -> tuple[int, int, str, str, str]:
    publication_order = {"mwb": 0, "wt": 1, "memorial": 2}
    return (
        -snapshot.monday.toordinal(),
        publication_order.get(snapshot.pub_type, 99),
        snapshot.language.casefold(),
        snapshot.issue.casefold(),
        snapshot.tree_key,
    )


def _json_fingerprint_default(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)
