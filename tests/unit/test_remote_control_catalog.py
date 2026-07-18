from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import json
from typing import Any
import uuid
from unittest.mock import patch

import pytest

from solin.core.media.formats import MediaKind
from solin.core.i18n.meeting_sections import display_meeting_section_title
from solin.core.meetings.models import MeetingMedia
from solin.core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
)
from solin.core.remote_control.catalog import (
    CatalogResolutionCode,
    CatalogResolutionError,
    MediaAvailability,
    RemoteCatalog,
    ResolvedMeetingPlay,
    ResolvedPlaylistPlay,
)
from solin.core.remote_control.contracts import (
    CatalogKind,
    PlayCommand,
    ProjectionOrigin,
    ProjectionSource,
    RemoteMeetingType,
)


class _PlaylistSource:
    def __init__(self, playlists: list[dict[str, Any]]) -> None:
        self.playlists = playlists

    def load(self) -> list[dict[str, Any]]:
        return self.playlists


def _play_command(
    source: ProjectionSource,
    collection_id: str,
    node_id: str,
    revision: int,
    *,
    start_paused: bool = False,
) -> PlayCommand:
    return PlayCommand(
        command_id=str(uuid.uuid4()),
        origin=ProjectionOrigin(source, collection_id, node_id),
        catalog_revision=revision,
        start_paused=start_paused,
    )


def _playlist() -> dict[str, Any]:
    return {
        "id": "playlist-1",
        "name": "Sunday meeting",
        "sections": [
            {
                "id": "main",
                "name": "Main",
                "position": 0,
                "slot_order": 0,
                "color_hue": 215,
                "collapsed": True,
            },
            {
                "id": "songs",
                "name": "Songs",
                "parent_id": "main",
                "position": 1,
                "slot_order": 0,
                "color_hue": 145,
            },
        ],
        "items": [
            {
                "id": "root-first",
                "title": "Welcome",
                "type": "image",
                "url": "C:/private/welcome.png",
            },
            {
                "id": "main-video",
                "title": "Introduction",
                "type": "video",
                "url": "https://private.invalid/introduction.mp4",
                "section_id": "main",
            },
            {
                "id": "song-audio",
                "title": "Song",
                "type": "audio",
                "url": "C:/private/song.mp3",
                "section_id": "songs",
                "base_duration_ticks": 65 * 10_000_000,
                "start_trim_ticks": 10 * 10_000_000,
                "end_trim_ticks": 5 * 10_000_000,
            },
            {
                "id": "root-last",
                "title": "Closing",
                "type": "video",
                "url": "C:/private/closing.mp4",
            },
        ],
        "markers": [
            {
                "id": "marker-second",
                "text": "Second note",
                "subsection_id": "songs",
                "position": 2,
                "slot_order": 1,
            },
            {
                "id": "marker-first",
                "text": "First note",
                "subsection_id": "songs",
                "position": 2,
                "slot_order": 0,
            },
        ],
    }


def test_local_media_resolution_stays_private_and_tracks_catalog_epoch(
    tmp_path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    playlist = {
        "id": "images",
        "name": "Images",
        "items": [
            {
                "id": "photo",
                "title": "Photo",
                "type": "image",
                "url": str(first),
            }
        ],
    }
    source = _PlaylistSource([playlist])
    catalog = RemoteCatalog(source, ())
    origin = ProjectionOrigin(ProjectionSource.PLAYLIST, "images", "photo")
    snapshot = catalog.snapshot()

    resolved = catalog.resolve_local_media(origin)
    assert resolved is not None
    assert resolved.location == str(first)
    assert resolved.extraction_kind is MediaKind.IMAGE
    assert resolved.placeholder_kind is MediaKind.IMAGE
    assert str(first) not in json.dumps(snapshot.to_dict())

    playlist["items"][0]["url"] = str(second)
    assert catalog.resolve_local_media(origin) is None
    catalog.snapshot()
    resolved = catalog.resolve_local_media(origin)
    assert resolved is not None
    assert resolved.location == str(second)
    assert resolved.extraction_kind is MediaKind.IMAGE
    assert resolved.placeholder_kind is MediaKind.IMAGE


def _meeting_snapshot() -> MeetingTreeSnapshot:
    return MeetingTreeSnapshot(
        tree_key="mwb:2026-07-13:E:1",
        pub_type="mwb",
        monday=date(2026, 7, 13),
        language="E",
        issue="1",
        nodes=[
            {
                "id": "meeting-section",
                "type": "section",
                "title": "Treasures",
                "color_hue": 32,
                "collapsed": True,
                "children": [
                    {
                        "id": "meeting-marker",
                        "type": "marker",
                        "text": "Opening",
                        "children": [],
                    },
                    {
                        "id": "meeting-media",
                        "type": "media",
                        "title": "C:\\private\\do-not-expose.mp4",
                        "media_type": "video",
                        "media_ref": {
                            "file_path": "C:\\private\\meeting.mp4",
                            "mime_type": "video/mp4",
                            "label": "Meeting video",
                            "multimedia_id": 42,
                        },
                        "base_duration_ticks": 90 * 10_000_000,
                        "start_trim_ticks": 5 * 10_000_000,
                        "children": [],
                    },
                ],
            }
        ],
        canonical_hash="canonical",
        deleted_source_keys=set(),
        linked_folder_files={"C:\\private\\meeting.mp4": "meeting-media"},
        meeting_folder_imports={},
        overview=MeetingTreeOverview(
            title="Midweek meeting",
            media_count=1,
            cover_bytes=b"persisted-publication-cover",
        ),
        revision=3,
    )


def test_meeting_catalog_localizes_only_canonical_generated_section_titles() -> None:
    snapshot = _meeting_snapshot()
    section = snapshot.nodes[0]
    section.update(
        {
            "title": "TREASURES FROM GOD'S WORD",
            "meeting_generated": True,
            "section_code": "tgw",
        }
    )
    catalog = RemoteCatalog(
        _PlaylistSource([]),
        [snapshot],
        meeting_group_title_resolver=display_meeting_section_title,
    )

    with patch(
        "solin.core.i18n.meeting_sections.translate_meeting_section_title",
        return_value="Treasures translated",
    ):
        assert catalog.snapshot().collections[0].nodes[0].title == "Treasures translated"

    section["title"] = "My custom section"
    section["user_title_override"] = True
    assert catalog.snapshot().collections[0].nodes[0].title == "My custom section"


def test_playlist_catalog_preserves_visual_hierarchy_and_slot_order() -> None:
    source = _PlaylistSource([_playlist()])

    def availability(
        _kind: CatalogKind,
        _collection_id: str,
        raw: Mapping[str, Any],
    ) -> MediaAvailability:
        if raw["id"] == "root-last":
            return MediaAvailability(False, "File unavailable")
        return MediaAvailability(True)

    catalog = RemoteCatalog(
        source,
        [],
        availability_resolver=availability,
        thumbnail_id_resolver=lambda _kind, _collection, raw: f"thumb-{raw['id']}",
    )

    snapshot = catalog.snapshot()
    collection = snapshot.collections[0]

    assert snapshot.catalog_revision == 1
    assert collection.kind is CatalogKind.PLAYLIST
    assert [node.id for node in collection.nodes] == [
        "main",
        "root-first",
        "root-last",
    ]
    section = collection.nodes[0]
    assert section.color == "#4179c7"
    assert section.collapsed is True
    assert [node.id for node in section.children] == ["songs", "main-video"]
    subsection = section.children[0]
    assert [node.id for node in subsection.children] == [
        "marker-first",
        "marker-second",
        "song-audio",
    ]
    media = subsection.children[-1]
    assert media.duration_ms == 50_000
    assert media.thumbnail_id == "thumb-song-audio"
    assert collection.nodes[-1].available is False
    assert collection.nodes[-1].unavailable_reason == "File unavailable"

    with pytest.raises(CatalogResolutionError) as unavailable_error:
        catalog.resolve_play(
            _play_command(
                ProjectionSource.PLAYLIST,
                collection.id,
                "root-last",
                snapshot.catalog_revision,
            )
        )
    assert unavailable_error.value.code is CatalogResolutionCode.NODE_NOT_PLAYABLE


def test_public_playlist_snapshot_never_contains_private_media_locations() -> None:
    snapshot = RemoteCatalog(_PlaylistSource([_playlist()]), []).snapshot().to_dict()

    encoded = json.dumps(snapshot, ensure_ascii=False)

    assert "C:/private" not in encoded
    assert "private.invalid" not in encoded
    assert '"url"' not in encoded
    assert "filePath" not in encoded
    assert "mediaRef" not in encoded


def test_playlist_resolution_returns_an_annotated_copy_only_inside_desktop() -> None:
    playlist = _playlist()
    source = _PlaylistSource([playlist])
    catalog = RemoteCatalog(source, [])
    revision = catalog.snapshot().catalog_revision

    resolved = catalog.resolve_play(
        _play_command(
            ProjectionSource.PLAYLIST,
            "playlist-1",
            "song-audio",
            revision,
            start_paused=True,
        )
    )

    assert isinstance(resolved, ResolvedPlaylistPlay)
    assert resolved.current_index == 2
    assert resolved.start_paused is True
    assert resolved.current_item["url"] == "C:/private/song.mp3"
    assert resolved.current_item["origin_kind"] == "playlist"
    assert resolved.current_item["origin_container_id"] == "playlist-1"
    assert resolved.current_item["origin_item_id"] == "song-audio"
    assert "origin_kind" not in playlist["items"][2]
    resolved.current_item["title"] = "Changed copy"
    assert playlist["items"][2]["title"] == "Song"


def test_private_playlist_reference_change_invalidates_catalog_revision() -> None:
    playlist = _playlist()
    source = _PlaylistSource([playlist])
    catalog = RemoteCatalog(source, [])
    snapshot = catalog.snapshot()
    command = _play_command(
        ProjectionSource.PLAYLIST,
        "playlist-1",
        "song-audio",
        snapshot.catalog_revision,
    )
    playlist["items"][2]["url"] = "C:/private/replaced.mp3"

    with pytest.raises(CatalogResolutionError) as exc_info:
        catalog.resolve_play(command)

    assert exc_info.value.code is CatalogResolutionCode.STALE_CATALOG
    assert catalog.snapshot().catalog_revision == snapshot.catalog_revision + 1


def test_meeting_catalog_and_resolution_keep_media_ref_private() -> None:
    snapshot = _meeting_snapshot()
    catalog = RemoteCatalog(_PlaylistSource([]), snapshot)

    public_snapshot = catalog.snapshot()
    collection = public_snapshot.collections[0]

    assert collection.kind is CatalogKind.MEETING
    assert collection.title == "Midweek meeting"
    assert collection.subtitle == ""
    assert collection.week_start == date(2026, 7, 13)
    assert collection.meeting_type is RemoteMeetingType.MIDWEEK
    assert collection.thumbnail_id is not None
    assert collection.to_dict()["weekStart"] == "2026-07-13"
    assert collection.to_dict()["meetingType"] == "midweek"
    assert collection.nodes[0].collapsed is True
    assert collection.to_dict()["nodes"][0]["collapsed"] is True
    assert [node.id for node in collection.nodes[0].children] == [
        "meeting-marker",
        "meeting-media",
    ]
    public_media = collection.nodes[0].children[1]
    assert public_media.title == "meeting-media"
    assert public_media.duration_ms == 85_000
    encoded = json.dumps(public_snapshot.to_dict(), ensure_ascii=False)
    assert "C:\\\\private" not in encoded
    assert "meeting.mp4" not in encoded
    assert "media_ref" not in encoded
    assert (
        catalog.resolve_collection_thumbnail(
            ProjectionSource.MEETING,
            snapshot.tree_key,
        )
        == b"persisted-publication-cover"
    )
    assert (
        catalog.resolve_collection_thumbnail(
            ProjectionSource.PLAYLIST,
            snapshot.tree_key,
        )
        is None
    )

    resolved = catalog.resolve_play(
        _play_command(
            ProjectionSource.MEETING,
            snapshot.tree_key,
            "meeting-media",
            public_snapshot.catalog_revision,
        )
    )

    assert isinstance(resolved, ResolvedMeetingPlay)
    assert isinstance(resolved.media, MeetingMedia)
    assert resolved.media.file_path == "C:\\private\\meeting.mp4"
    assert resolved.media.origin_kind == "meeting"
    assert resolved.media.origin_container_id == snapshot.tree_key
    assert resolved.media.origin_item_id == "meeting-media"


def test_non_media_node_and_path_shaped_thumbnail_are_rejected() -> None:
    snapshot = _meeting_snapshot()
    catalog = RemoteCatalog(_PlaylistSource([]), snapshot)
    revision = catalog.snapshot().catalog_revision

    with pytest.raises(CatalogResolutionError) as node_error:
        catalog.resolve_play(
            _play_command(
                ProjectionSource.MEETING,
                snapshot.tree_key,
                "meeting-section",
                revision,
            )
        )
    assert node_error.value.code is CatalogResolutionCode.NODE_NOT_PLAYABLE

    leaking_catalog = RemoteCatalog(
        _PlaylistSource([_playlist()]),
        [],
        thumbnail_id_resolver=lambda _kind, _collection, _raw: "C:/secret/thumb.jpg",
    )
    with pytest.raises(CatalogResolutionError) as thumbnail_error:
        leaking_catalog.snapshot()
    assert thumbnail_error.value.code is CatalogResolutionCode.INVALID_CATALOG

    reason_catalog = RemoteCatalog(
        _PlaylistSource([_playlist()]),
        [],
        availability_resolver=lambda _kind, _collection, _raw: MediaAvailability(
            False,
            "Missing C:\\private\\file.mp4",
        ),
    )
    public_reason = reason_catalog.snapshot().collections[0].nodes[1]
    assert public_reason.unavailable_reason == "Unavailable"


def test_duplicate_meeting_node_ids_get_stable_remote_only_ids() -> None:
    snapshot = _meeting_snapshot()
    duplicate = deepcopy(snapshot.nodes[0]["children"][1])
    duplicate["title"] = "Second occurrence"
    duplicate["media_ref"]["file_path"] = "C:\\private\\second.mp4"
    snapshot.nodes[0]["children"].append(duplicate)
    catalog = RemoteCatalog(_PlaylistSource([]), snapshot)

    first_snapshot = catalog.snapshot()
    children = first_snapshot.collections[0].nodes[0].children
    first_id = children[1].id
    duplicate_id = children[2].id

    assert first_id == "meeting-media"
    assert duplicate_id != first_id
    assert catalog.snapshot().collections[0].nodes[0].children[2].id == duplicate_id

    resolved = catalog.resolve_play(
        _play_command(
            ProjectionSource.MEETING,
            snapshot.tree_key,
            duplicate_id,
            first_snapshot.catalog_revision,
        )
    )

    assert isinstance(resolved, ResolvedMeetingPlay)
    assert resolved.media.file_path == "C:\\private\\second.mp4"
    assert resolved.media.origin_item_id == duplicate_id


def test_catalog_accepts_the_authoritative_meeting_tree_store(tmp_path) -> None:
    snapshot = _meeting_snapshot()
    store = MeetingTreeStore(tmp_path / "meeting_trees.json")
    store.save(
        snapshot.tree_key,
        snapshot.nodes,
        snapshot.canonical_hash,
        overview=snapshot.overview,
    )

    catalog_snapshot = RemoteCatalog(_PlaylistSource([]), store).snapshot()

    assert [collection.id for collection in catalog_snapshot.collections] == [snapshot.tree_key]


def test_linked_folder_playlists_keep_distinct_public_source_and_private_queue() -> None:
    linked = _playlist()
    linked["id"] = "linked-folder-1"
    catalog = RemoteCatalog(
        _PlaylistSource([]),
        [],
        linked_playlist_source=lambda: [linked],
    )
    snapshot = catalog.snapshot()

    assert snapshot.collections[0].kind is CatalogKind.LINKED_FOLDER
    resolved = catalog.resolve_play(
        _play_command(
            ProjectionSource.LINKED_FOLDER,
            "linked-folder-1",
            "song-audio",
            snapshot.catalog_revision,
        )
    )

    assert isinstance(resolved, ResolvedPlaylistPlay)
    assert resolved.current_item["origin_kind"] == "linked_folder"
    assert resolved.current_item["url"] == "C:/private/song.mp3"
