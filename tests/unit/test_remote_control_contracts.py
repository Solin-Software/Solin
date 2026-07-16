from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
import json
import uuid

import pytest

from solin.core.remote_control.contracts import (
    CatalogCollection,
    CatalogKind,
    CatalogNode,
    CatalogNodeKind,
    CatalogSnapshot,
    CommandError,
    CommandErrorCode,
    CommandResult,
    NextCommand,
    PauseCommand,
    PlaybackCapabilities,
    PlaybackState,
    PlayCommand,
    PreviousCommand,
    ProjectionOrigin,
    ProjectionQueueItem,
    ProjectionSnapshot,
    ProjectionSource,
    RemoteMeetingType,
    RemoteMediaKind,
    ResumeCommand,
    SeekCommand,
    SetVolumeCommand,
    StopCommand,
    parse_projection_command,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _valid_origin() -> ProjectionOrigin:
    return ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1")


def test_projection_origin_is_typed_immutable_and_json_safe() -> None:
    origin = _valid_origin()

    assert origin.to_dict() == {
        "source": "playlist",
        "collectionId": "playlist-1",
        "nodeId": "media-1",
    }
    assert ProjectionOrigin.from_dict(origin.to_dict()) == origin
    assert json.loads(json.dumps(origin.to_dict())) == origin.to_dict()
    with pytest.raises(FrozenInstanceError):
        origin.node_id = "changed"  # type: ignore[misc]


def test_projection_origin_rejects_untyped_and_incomplete_catalog_identity() -> None:
    with pytest.raises(TypeError, match="ProjectionSource"):
        ProjectionOrigin(
            source="playlist",  # type: ignore[arg-type] - deliberately invalid boundary value
            collection_id="playlist-1",
            node_id="media-1",
        )
    with pytest.raises(ValueError, match="collection_id and node_id"):
        ProjectionOrigin(ProjectionSource.MEETING, "week-1")

    temporary = ProjectionOrigin(ProjectionSource.TEMPORARY)
    assert temporary.to_dict()["collectionId"] is None


def test_catalog_tree_uses_public_display_metadata_only_and_serializes_recursively() -> None:
    media = CatalogNode(
        id="media-1",
        kind=CatalogNodeKind.MEDIA,
        title="Opening video",
        media_kind=RemoteMediaKind.VIDEO,
        duration_ms=12_000,
        thumbnail_id="thumb-1",
    )
    section = CatalogNode(
        id="section-1",
        kind=CatalogNodeKind.SECTION,
        title="Opening Comments",
        children=(media,),
        color="#407BFF",
        collapsed=True,
    )
    catalog = CatalogSnapshot(
        catalog_revision=7,
        collections=(
            CatalogCollection(
                id="playlist-1",
                kind=CatalogKind.PLAYLIST,
                title="Public meeting",
                subtitle="2 items",
                thumbnail_id="collection-cover-1",
                nodes=(section,),
            ),
        ),
    )

    payload = catalog.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["catalogRevision"] == 7
    assert payload["collections"][0]["thumbnailId"] == "collection-cover-1"  # type: ignore[index]
    assert payload["collections"][0]["nodes"][0]["collapsed"] is True  # type: ignore[index]
    assert payload["collections"][0]["nodes"][0]["children"][0]["id"] == "media-1"  # type: ignore[index]
    assert "file_path" not in encoded
    assert "media_ref" not in encoded
    assert "url" not in encoded.lower()


def test_catalog_rejects_duplicate_ids_and_media_children() -> None:
    child = CatalogNode("same", CatalogNodeKind.MEDIA, "Media")

    with pytest.raises(ValueError, match="media nodes cannot"):
        CatalogNode("parent", CatalogNodeKind.MEDIA, "Parent", children=(child,))
    with pytest.raises(TypeError, match="collapsed must be a boolean"):
        CatalogNode(
            "section",
            CatalogNodeKind.SECTION,
            "Section",
            collapsed="yes",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="duplicate catalog node id"):
        CatalogCollection(
            "playlist",
            CatalogKind.PLAYLIST,
            "Playlist",
            nodes=(child, child),
        )
    with pytest.raises(ValueError, match="collection ids"):
        CatalogSnapshot(
            1,
            (
                CatalogCollection("same", CatalogKind.PLAYLIST, "One"),
                CatalogCollection(
                    "same",
                    CatalogKind.MEETING,
                    "Two",
                    week_start=date(2026, 7, 13),
                    meeting_type=RemoteMeetingType.MIDWEEK,
                ),
            ),
        )
    with pytest.raises(ValueError, match="only valid for meetings"):
        CatalogCollection(
            "playlist",
            CatalogKind.PLAYLIST,
            "Playlist",
            week_start=date(2026, 7, 13),
            meeting_type=RemoteMeetingType.MIDWEEK,
        )
    with pytest.raises(ValueError, match="require week_start and meeting_type"):
        CatalogCollection("meeting", CatalogKind.MEETING, "Meeting")


def test_projection_snapshot_contains_queue_transport_and_capabilities() -> None:
    origin = _valid_origin()
    capabilities = PlaybackCapabilities(
        can_pause=True,
        can_seek=True,
        can_set_volume=True,
        can_next=True,
        can_stop=True,
    )
    snapshot = ProjectionSnapshot(
        playback_revision=11,
        state=PlaybackState.PLAYING,
        playback_session_id="playback-1",
        origin=origin,
        title="Opening video",
        media_kind=RemoteMediaKind.VIDEO,
        queue=(
            ProjectionQueueItem(
                origin,
                "Opening video",
                RemoteMediaKind.VIDEO,
                thumbnail_id="thumb-1",
            ),
        ),
        current_index=0,
        position_ms=3_500,
        duration_ms=12_000,
        volume=72,
        capabilities=capabilities,
    )

    payload = snapshot.to_dict()

    assert payload["playbackRevision"] == 11
    assert payload["playbackSessionId"] == "playback-1"
    assert payload["positionMs"] == 3_500
    assert payload["queue"][0]["thumbnailId"] == "thumb-1"  # type: ignore[index]
    assert payload["capabilities"] == {
        "pause": True,
        "resume": False,
        "seek": True,
        "setVolume": True,
        "previous": False,
        "next": True,
        "stop": True,
    }
    json.dumps(payload)


def test_projection_snapshot_enforces_consistent_idle_queue_and_timeline_state() -> None:
    assert ProjectionSnapshot.idle().to_dict()["state"] == "idle"

    with pytest.raises(ValueError, match="requires playback_session_id"):
        ProjectionSnapshot(0, PlaybackState.PLAYING)
    with pytest.raises(ValueError, match="cannot have a session"):
        ProjectionSnapshot(0, PlaybackState.IDLE, playback_session_id="stale")
    with pytest.raises(ValueError, match="current_index"):
        ProjectionSnapshot(
            0,
            PlaybackState.PLAYING,
            playback_session_id="session",
            queue=(ProjectionQueueItem(_valid_origin(), "Media", RemoteMediaKind.VIDEO),),
        )
    with pytest.raises(ValueError, match="must not exceed duration"):
        ProjectionSnapshot(
            0,
            PlaybackState.PLAYING,
            playback_session_id="session",
            position_ms=2,
            duration_ms=1,
        )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            lambda command_id: PlayCommand(command_id, _valid_origin(), 5, True),
            {
                "type": "play",
                "catalogRevision": 5,
                "origin": _valid_origin().to_dict(),
                "startPaused": True,
            },
        ),
        (lambda command_id: PauseCommand(command_id, "session"), {"type": "pause"}),
        (lambda command_id: ResumeCommand(command_id, "session"), {"type": "resume"}),
        (
            lambda command_id: SeekCommand(command_id, "session", 1_500),
            {"type": "seek", "positionMs": 1_500},
        ),
        (
            lambda command_id: SetVolumeCommand(command_id, "session", 42),
            {"type": "set_volume", "volume": 42},
        ),
        (lambda command_id: PreviousCommand(command_id, "session"), {"type": "previous"}),
        (lambda command_id: NextCommand(command_id, "session"), {"type": "next"}),
        (lambda command_id: StopCommand(command_id), {"type": "stop"}),
    ],
)
def test_all_commands_round_trip_through_the_strict_wire_parser(command, expected) -> None:
    original = command(_uuid())
    payload = original.to_dict()

    assert parse_projection_command(payload) == original
    for key, value in expected.items():
        assert payload[key] == value
    if payload["type"] not in {"play", "stop"}:
        assert payload["playbackSessionId"] == "session"
    json.dumps(payload)


def test_command_contract_rejects_non_uuid_ids_and_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="UUID"):
        StopCommand("not-a-uuid")
    with pytest.raises(ValueError, match="temporary"):
        PlayCommand(_uuid(), ProjectionOrigin(ProjectionSource.TEMPORARY), 0)
    with pytest.raises(ValueError, match="not exceed 100"):
        SetVolumeCommand(_uuid(), "session", 101)
    with pytest.raises(TypeError, match="integer"):
        parse_projection_command(
            {
                "commandId": _uuid(),
                "type": "seek",
                "playbackSessionId": "session",
                "positionMs": True,
            }
        )
    with pytest.raises(ValueError, match="unknown command field"):
        parse_projection_command(
            {
                "commandId": _uuid(),
                "type": "stop",
                "filePath": "C:/private/media.mp4",
            }
        )


def test_command_result_has_an_unambiguous_success_or_sanitized_error() -> None:
    command_id = _uuid()
    success = CommandResult.success(command_id, catalog_revision=2, playback_revision=8)
    failure = CommandResult.failure(
        command_id,
        catalog_revision=2,
        playback_revision=8,
        error=CommandError(
            CommandErrorCode.UNAVAILABLE,
            "Media is not available.",
            retryable=True,
        ),
    )

    assert success.to_dict()["error"] is None
    assert failure.to_dict()["error"] == {
        "code": "unavailable",
        "message": "Media is not available.",
        "retryable": True,
    }
    json.dumps((success.to_dict(), failure.to_dict()))

    with pytest.raises(ValueError, match="failed results require"):
        CommandResult(command_id, False, 0, 0)
