"""Typed contracts for routing media to playlists and meeting trees."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from solin.core.media.placement import END_OF_LIST_INDEX


class MediaRouteAction(str, Enum):
    PLAY = "play"
    ADD = "add"


class MediaDestinationKind(str, Enum):
    PLAYLIST = "playlist"
    MEETING = "meeting"


@dataclass(frozen=True, slots=True)
class MediaDestinationAsset:
    """One ordered source in a destination request."""

    title: str
    source_id: str
    item: Mapping[str, Any] | None = None
    import_path: str = ""
    import_kind: str = ""

    def __post_init__(self) -> None:
        has_item = self.item is not None
        has_import = bool(self.import_path)
        if has_item == has_import:
            raise ValueError("An asset must contain exactly one item or import path")


@dataclass(frozen=True, slots=True)
class MediaDestinationRequest:
    title: str
    assets: tuple[MediaDestinationAsset, ...] = field(default_factory=tuple)
    can_play: bool = False

    @property
    def item_count(self) -> int:
        return len(self.assets) or 1


def create_media_destination_request(
    item: Mapping[str, Any],
) -> MediaDestinationRequest | None:
    """Build a single-item request with an isolated destination payload."""

    destination_item = copy.deepcopy(dict(item))
    source_id = str(destination_item.get("url") or "")
    if not source_id:
        return None
    title = str(destination_item.get("title") or "")
    return MediaDestinationRequest(
        title=title,
        assets=(
            MediaDestinationAsset(
                title=title,
                source_id=source_id,
                item=destination_item,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class PlaylistDestinationTarget:
    playlist_id: str
    playlist_name: str
    create_new: bool = False
    list_id: str = "root"
    insert_index: int = END_OF_LIST_INDEX


@dataclass(frozen=True, slots=True)
class MeetingDestinationTarget:
    monday: str
    pub_type: str
    list_id: str
    insert_index: int


MediaDestinationTarget = PlaylistDestinationTarget | MeetingDestinationTarget


@dataclass(frozen=True, slots=True)
class PreparedMediaBatch:
    items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    handled_sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MediaDestinationSelection:
    action: MediaRouteAction
    destination: MediaDestinationKind | None = None
    target: MediaDestinationTarget | None = None


@dataclass(frozen=True, slots=True)
class MediaDestinationOutcome:
    added_count: int
    referenced_urls: tuple[str, ...] = field(default_factory=tuple)
    handled_sources: tuple[str, ...] = field(default_factory=tuple)
    destination_accepted: bool = True


__all__ = [
    "create_media_destination_request",
    "MediaDestinationKind",
    "MediaDestinationAsset",
    "MediaDestinationOutcome",
    "MediaDestinationRequest",
    "MediaDestinationSelection",
    "MediaRouteAction",
    "MeetingDestinationTarget",
    "PlaylistDestinationTarget",
    "PreparedMediaBatch",
]
