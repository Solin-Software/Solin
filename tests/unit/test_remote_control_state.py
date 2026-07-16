from __future__ import annotations

import json
import uuid

import pytest

from solin.core.remote_control.contracts import (
    CatalogCollection,
    CatalogKind,
    CommandError,
    CommandErrorCode,
    NextCommand,
    PauseCommand,
    PlaybackState,
    PlayCommand,
    ProjectionOrigin,
    ProjectionSnapshot,
    RemoteMediaKind,
    ResumeCommand,
    SeekCommand,
    SetVolumeCommand,
    StopCommand,
)
from solin.core.remote_control.contracts import ProjectionSource
from solin.core.remote_control.state import (
    CommandDisposition,
    ProjectionCommandSession,
    RemoteControlStateStore,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _origin() -> ProjectionOrigin:
    return ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1")


def _playing(*, session_id: str = "session-1", revision: int = 0) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        playback_revision=revision,
        state=PlaybackState.PLAYING,
        playback_session_id=session_id,
        origin=_origin(),
        title="Media",
        media_kind=RemoteMediaKind.VIDEO,
        duration_ms=10_000,
    )


def test_state_store_creates_one_server_instance_and_json_safe_initial_snapshots() -> None:
    state = RemoteControlStateStore()
    snapshot = state.snapshot()

    assert uuid.UUID(snapshot.server_instance_id)
    assert snapshot.catalog_revision == 0
    assert snapshot.playback_revision == 0
    assert snapshot.playback.state is PlaybackState.IDLE
    assert snapshot is state.snapshot()
    json.dumps(snapshot.to_dict())


def test_server_instance_rotation_preserves_catalog_and_playback_state() -> None:
    state = RemoteControlStateStore()
    before = state.snapshot()

    rotated = state.rotate_server_instance()
    after = state.snapshot()

    assert rotated != before.server_instance_id
    assert after.server_instance_id == rotated
    assert after.catalog == before.catalog
    assert after.playback == before.playback


def test_catalog_revision_is_monotonic_and_identical_updates_are_noops() -> None:
    state = RemoteControlStateStore()
    collections = (CatalogCollection("playlist-1", CatalogKind.PLAYLIST, "One"),)

    first = state.update_catalog(collections)
    duplicate = state.update_catalog(collections)
    second = state.update_catalog(
        (CatalogCollection("playlist-1", CatalogKind.PLAYLIST, "Renamed"),)
    )

    assert first.catalog_revision == 1
    assert duplicate is first
    assert second.catalog_revision == 2
    assert state.catalog is second


def test_catalog_private_change_token_advances_revision_without_public_changes() -> None:
    state = RemoteControlStateStore()

    first = state.update_catalog((), change_token="private-v1")
    duplicate = state.update_catalog((), change_token="private-v1")
    second = state.update_catalog((), change_token="private-v2")

    assert first.catalog_revision == 1
    assert duplicate is first
    assert second.catalog_revision == 2


def test_playback_revision_is_owned_by_store_and_identical_updates_are_noops() -> None:
    state = RemoteControlStateStore()

    first = state.update_playback(_playing(revision=900))
    duplicate = state.update_playback(_playing(revision=1))
    paused = state.update_playback(
        ProjectionSnapshot(
            playback_revision=0,
            state=PlaybackState.PAUSED,
            playback_session_id="session-1",
            origin=_origin(),
            title="Media",
            media_kind=RemoteMediaKind.VIDEO,
            duration_ms=10_000,
        )
    )

    assert first.playback_revision == 1
    assert duplicate is first
    assert paused.playback_revision == 2
    assert state.snapshot().playback is paused


def test_play_requires_the_current_catalog_revision_and_replays_the_rejection() -> None:
    state = RemoteControlStateStore()
    state.update_catalog((CatalogCollection("playlist-1", CatalogKind.PLAYLIST, "One"),))
    session = ProjectionCommandSession(state)
    command = PlayCommand(_uuid(), _origin(), catalog_revision=0)

    rejected = session.prepare(command)
    replayed = session.prepare(command)

    assert rejected.disposition is CommandDisposition.REJECTED
    assert rejected.result is not None
    assert rejected.result.error is not None
    assert rejected.result.error.code is CommandErrorCode.CATALOG_STALE
    assert rejected.result.catalog_revision == 1
    assert replayed.disposition is CommandDisposition.REPLAYED
    assert replayed.result is rejected.result


@pytest.mark.parametrize(
    "command_factory",
    [
        PauseCommand,
        ResumeCommand,
        lambda command_id, session_id: SeekCommand(command_id, session_id, 100),
        lambda command_id, session_id: SetVolumeCommand(command_id, session_id, 50),
        NextCommand,
    ],
)
def test_transport_commands_require_the_current_playback_session(command_factory) -> None:
    state = RemoteControlStateStore(playback=_playing(session_id="current"))
    session = ProjectionCommandSession(state)
    command = command_factory(_uuid(), "stale")

    decision = session.prepare(command)

    assert decision.disposition is CommandDisposition.REJECTED
    assert decision.result is not None
    assert decision.result.error is not None
    assert decision.result.error.code is CommandErrorCode.PLAYBACK_STALE


def test_stop_is_unconditional_and_session_controls_accept_the_current_session() -> None:
    state = RemoteControlStateStore(playback=_playing(session_id="current"))
    session = ProjectionCommandSession(state)

    pause = PauseCommand(_uuid(), "current")
    stop = StopCommand(_uuid())

    assert session.prepare(pause).should_execute is True
    assert session.prepare(stop).should_execute is True


def test_reserved_command_cannot_execute_twice_and_completion_is_replayed_exactly() -> None:
    state = RemoteControlStateStore(playback=_playing())
    session = ProjectionCommandSession(state)
    command = PauseCommand(_uuid(), "session-1")

    accepted = session.prepare(command)
    pending_retry = session.prepare(command)
    state.update_playback(
        ProjectionSnapshot(
            playback_revision=0,
            state=PlaybackState.PAUSED,
            playback_session_id="session-1",
            origin=_origin(),
            title="Media",
            media_kind=RemoteMediaKind.VIDEO,
            duration_ms=10_000,
        )
    )
    completed = session.complete_success(command)
    replay = session.prepare(command)

    assert accepted.should_execute is True
    assert pending_retry.disposition is CommandDisposition.IN_PROGRESS
    assert pending_retry.result is not None
    assert pending_retry.result.error is not None
    assert pending_retry.result.error.code is CommandErrorCode.COMMAND_IN_PROGRESS
    assert completed.playback_revision == 1
    assert replay.disposition is CommandDisposition.REPLAYED
    assert replay.result is completed


def test_reusing_a_command_id_with_different_payload_is_rejected() -> None:
    state = RemoteControlStateStore(playback=_playing())
    session = ProjectionCommandSession(state)
    command_id = _uuid()
    original = SeekCommand(command_id, "session-1", 100)
    conflicting = SeekCommand(command_id, "session-1", 200)

    assert session.prepare(original).should_execute is True
    conflict = session.prepare(conflicting)

    assert conflict.disposition is CommandDisposition.REJECTED
    assert conflict.result is not None
    assert conflict.result.error is not None
    assert conflict.result.error.code is CommandErrorCode.COMMAND_ID_CONFLICT


def test_failed_execution_is_cached_for_idempotent_retry() -> None:
    state = RemoteControlStateStore(playback=_playing())
    session = ProjectionCommandSession(state)
    command = PauseCommand(_uuid(), "session-1")
    error = CommandError(CommandErrorCode.BLOCKED, "Playback protection is active.")

    assert session.prepare(command).should_execute
    result = session.complete_failure(command, error)
    replay = session.prepare(command)

    assert result.ok is False
    assert replay.disposition is CommandDisposition.REPLAYED
    assert replay.result is result


def test_deduplication_cache_is_bounded_without_evicting_pending_commands() -> None:
    state = RemoteControlStateStore(playback=_playing())
    session = ProjectionCommandSession(state, deduplication_limit=1)
    first = PauseCommand(_uuid(), "session-1")
    second = PauseCommand(_uuid(), "session-1")

    assert session.prepare(first).should_execute
    full = session.prepare(second)
    assert full.result is not None
    assert full.result.error is not None
    assert full.result.error.code is CommandErrorCode.UNAVAILABLE
    assert session.cached_command_count == 1

    session.complete_success(first)
    assert session.prepare(second).should_execute
    assert session.cached_command_count == 1


def test_completion_rejects_unreserved_or_already_completed_commands() -> None:
    state = RemoteControlStateStore(playback=_playing())
    session = ProjectionCommandSession(state)
    command = PauseCommand(_uuid(), "session-1")

    with pytest.raises(ValueError, match="not reserved"):
        session.complete_success(command)

    assert session.prepare(command).should_execute
    session.complete_success(command)
    with pytest.raises(ValueError, match="already completed"):
        session.complete_success(command)
