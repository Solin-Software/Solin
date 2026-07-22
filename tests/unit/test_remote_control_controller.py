from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any
import uuid
from unittest.mock import patch
from datetime import timedelta

from PIL import Image
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer
import pytest

from solin.controllers.remote_control_controller import (
    RemoteControlController,
    RemoteControlDependencies,
)
from solin.core.media.formats import MediaKind
from solin.core.media.thumbnail_identity import thumbnail_storage_id
from solin.core.meetings.tree_store import MeetingTreeOverview, MeetingTreeStore
from solin.core.remote_control.contracts import (
    CatalogCollection,
    CatalogKind,
    CatalogNode,
    CatalogNodeKind,
    CommandErrorCode,
    PauseCommand,
    PlayCommand,
    PlaybackState,
    ProjectionOrigin,
    ProjectionSource,
    RemoteMediaKind,
)
from solin.core.remote_control.certificates import TLSCertificateStore
from solin.core.remote_control.state import ProjectionCommandSession
from solin.core.remote_control.thumbnails import render_media_placeholder_thumbnail


_APP = QCoreApplication.instance() or QCoreApplication([])


class _Repository:
    def __init__(self, values: list[dict[str, Any]] | None = None) -> None:
        self.values = values or []
        self.listeners = []

    def load(self) -> list[dict[str, Any]]:
        return self.values

    def load_all(self) -> dict[str, Any]:
        return {"trees": {}}

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class _WatchedStore(_Repository):
    def scan_root(self, _path: str) -> list[dict[str, Any]]:
        return []

    def load_playlist(self, _path: str) -> dict[str, Any]:
        raise AssertionError("No watched folder should be loaded")


class _ThumbnailStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, item_id: str) -> bool:
        return self.path(item_id).is_file()

    def path(self, item_id: str) -> Path:
        return self.root / f"{item_id}.jpg"

    def save_bytes(self, item_id: str, data: bytes) -> Path:
        path = self.path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class _MediaThumbnailExtractor:
    def __init__(self) -> None:
        self.requests: list[tuple[str, MediaKind]] = []
        self.results: dict[MediaKind, bytes | None] = {}
        self.delay = 0.0
        self.stopped = False

    async def extract(self, location: str, media_kind: MediaKind) -> bytes | None:
        self.requests.append((location, media_kind))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.get(media_kind)

    def shutdown(self) -> None:
        self.stopped = True


class _ProjectionSession:
    def __init__(self) -> None:
        self.state_type = "idle"
        self.state = {"type": "idle"}
        self.session_id = 0
        self.listeners = []

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class _Player:
    def __init__(self) -> None:
        self.state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):
        return self.state


class _MediaController(QObject):
    state_changed = Signal(object)
    duration_changed = Signal(int)
    position_changed = Signal(int)
    playback_recovery_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.player = _Player()
        self.duration = 0
        self.position = 0
        self.session_id = 0
        self.is_recovering = False

    def play(self) -> None:
        self.player.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.player.state = QMediaPlayer.PlaybackState.PausedState


class _ProjectionBar(QObject):
    volume_changed = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self.playlist_index = 0
        self.volume_percent = 72

    def playlist_items(self) -> list[dict[str, Any]]:
        return list(self.items)

    def current_media_title(self) -> str:
        return "Fallback title"

    def can_navigate_previous(self) -> bool:
        return self.playlist_index > 0

    def can_navigate_next(self) -> bool:
        return self.playlist_index < len(self.items) - 1

    def navigate_previous(self) -> bool:
        return False

    def navigate_next(self) -> bool:
        return False

    def set_volume_percent(self, value: int) -> None:
        self.volume_percent = value


class _SettingsWidget(QObject):
    remote_control_settings_changed = Signal()
    remote_control_credentials_changed = Signal()
    remote_control_revoke_requested = Signal()
    watched_folder_changed = Signal(str)

    def set_remote_control_runtime_status(self, **_values: Any) -> None:
        pass


class _PlaybackProtection(QObject):
    lockedChanged = Signal()
    locked = False

    def __init__(self) -> None:
        super().__init__()

    def allow_manual_projection_change(self, *, notify: bool) -> bool:
        del notify
        return True

    def request_seek(self, _position: int) -> bool:
        return True


class _MediaProjection:
    def __init__(self) -> None:
        self.projected: tuple[list[dict[str, Any]], int] | None = None

    def project_media_at_index(self, items: list[dict[str, Any]], index: int) -> None:
        self.projected = (items, index)

    def on_meeting_media_project(self, _media: object) -> None:
        pass


def _controller(tmp_path: Path, playlists: list[dict[str, Any]]):
    playlist_repository = _Repository(playlists)
    meeting_repository = MeetingTreeStore(tmp_path / "meeting-trees.json")
    watched_store = _WatchedStore()
    projection_session = _ProjectionSession()
    projection_bar = _ProjectionBar()
    media_controller = _MediaController()
    media_projection = _MediaProjection()
    media_thumbnail_extractor = _MediaThumbnailExtractor()
    controller = RemoteControlController(
        RemoteControlDependencies(
            runtime_paths=SimpleNamespace(data_dir=tmp_path),
            active_profile_id="profile-1",
            active_profile_name="Main hall",
            active_profile_locale=lambda: "en",
            settings=SimpleNamespace(
                enabled=lambda: False,
                network_selection=lambda: None,
            ),
            credentials=SimpleNamespace(has_credentials=lambda: False),
            playlist_repository=playlist_repository,
            meeting_tree_store=meeting_repository,
            watched_folder_settings=SimpleNamespace(path=lambda: ""),
            watched_folder_playlist_store=watched_store,
            playlist_thumbnail_store=_ThumbnailStore(tmp_path / "playlist-thumbs"),
            meeting_thumbnail_store=_ThumbnailStore(tmp_path / "meeting-thumbs"),
            media_thumbnail_extractor=media_thumbnail_extractor,
            projection_session=projection_session,
            projection_bar=projection_bar,
            media_controller=media_controller,
            media_projection=media_projection,
            projection_stop=SimpleNamespace(stop_projection=lambda: None),
            playback_protection=_PlaybackProtection(),
            settings_widget=_SettingsWidget(),
        ),
        None,
    )
    return controller, projection_session, projection_bar, media_controller, media_projection


def test_catalog_revision_stays_aligned_when_initial_public_catalog_is_empty(
    tmp_path: Path,
) -> None:
    controller, *_ = _controller(tmp_path, [])

    controller._refresh_catalog()

    assert controller.state.catalog_revision == 1
    controller.stop()


def test_invalid_remote_catalog_does_not_escape_into_app_startup(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])
    controller._catalog = SimpleNamespace(
        build=lambda: (_ for _ in ()).throw(ValueError("duplicate node"))
    )

    controller._refresh_catalog()

    assert controller.state.catalog_revision == 0
    controller.stop()


def test_clearing_watched_folder_invalidates_remote_catalog(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])
    initial_generation = controller._catalog_refresh_generation

    controller._dependencies.settings_widget.watched_folder_changed.emit("")

    assert controller._catalog_refresh_generation == initial_generation + 1
    assert controller._catalog_refresh_timer.isActive()
    controller._catalog_refresh_timer.stop()
    controller.stop()


def test_repository_catalog_refresh_runs_outside_qt_thread(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []
    original_build = controller._catalog.build

    def build():
        worker_threads.append(threading.get_ident())
        return original_build()

    controller._catalog.build = build
    controller._schedule_catalog_refresh()
    controller._catalog_refresh_timer.stop()
    controller._start_catalog_refresh()

    deadline = time.monotonic() + 2
    while controller._catalog_refresh_inflight is not None:
        _APP.processEvents()
        time.sleep(0.001)
        assert time.monotonic() < deadline

    assert worker_threads
    assert all(thread_id != caller_thread for thread_id in worker_threads)
    assert controller.state.catalog_revision == 1
    controller.stop()


def test_catalog_invalidation_during_scan_keeps_single_debounced_followup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, *_ = _controller(tmp_path, [])
    scheduled = []
    monkeypatch.setattr(
        "solin.controllers.remote_control_controller.QTimer.singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    controller._catalog_refresh_generation = 2
    controller._catalog_refresh_inflight = (1, object())
    controller._catalog_refresh_pending = False
    controller._catalog_refresh_timer.start()

    controller._on_catalog_build_completed(1, None, None)

    assert controller._catalog_refresh_timer.isActive()
    assert scheduled == []
    controller._catalog_refresh_timer.stop()
    controller._catalog_refresh_inflight = None
    controller.stop()


def test_superseded_catalog_build_cannot_replace_the_published_private_view(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    playlist = {
        "id": "playlist-1",
        "name": "Program",
        "items": [
            {
                "id": "media-1",
                "title": "Opening",
                "type": "video",
                "url": str(first_path),
            }
        ],
    }
    controller, *_rest, media_projection = _controller(tmp_path, [playlist])
    controller._refresh_catalog()
    published_revision = controller.state.catalog_revision
    playlist["items"][0]["url"] = str(second_path)
    superseded = controller._catalog.build()
    controller._catalog_refresh_generation = 2
    controller._catalog_refresh_inflight = (1, object())
    controller._catalog_refresh_timer.start()

    controller._on_catalog_build_completed(1, superseded, None)

    command = PlayCommand(
        str(uuid.uuid4()),
        ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1"),
        published_revision,
    )
    assert controller._execute_command(command) is None
    assert media_projection.projected is not None
    assert media_projection.projected[0][0]["url"] == str(first_path)
    assert controller.state.catalog_revision == published_revision
    controller._catalog_refresh_timer.stop()
    controller.stop()


def test_synchronous_catalog_refresh_supersedes_an_inflight_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    third_path = tmp_path / "third.mp4"
    for path in (first_path, second_path, third_path):
        path.write_bytes(path.stem.encode())
    playlist = {
        "id": "playlist-1",
        "name": "Program",
        "items": [
            {
                "id": "media-1",
                "title": "Opening",
                "type": "video",
                "url": str(first_path),
            }
        ],
    }
    controller, *_rest, media_projection = _controller(tmp_path, [playlist])
    controller._refresh_catalog()

    playlist["items"][0]["url"] = str(second_path)
    superseded = controller._catalog.build()
    inflight_generation = controller._catalog_refresh_generation
    controller._catalog_refresh_inflight = (inflight_generation, object())
    scheduled = []
    monkeypatch.setattr(
        "solin.controllers.remote_control_controller.QTimer.singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    playlist["items"][0]["url"] = str(third_path)
    controller._refresh_catalog()
    published_revision = controller.state.catalog_revision
    controller._on_catalog_build_completed(inflight_generation, superseded, None)

    command = PlayCommand(
        str(uuid.uuid4()),
        ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1"),
        published_revision,
    )
    assert controller._execute_command(command) is None
    assert media_projection.projected is not None
    assert media_projection.projected[0][0]["url"] == str(third_path)
    assert controller.state.catalog_revision == published_revision
    assert len(scheduled) == 1
    controller.stop()


def test_catalog_commit_is_atomic_for_public_and_private_readers(tmp_path: Path) -> None:
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"video")
    playlist = {
        "id": "playlist-1",
        "name": "Program",
        "items": [
            {
                "id": "media-1",
                "title": "Opening",
                "type": "video",
                "url": str(media_path),
            }
        ],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    build = controller._catalog.build()
    public_published = threading.Event()
    allow_private_publish = threading.Event()
    reader_completed = threading.Event()
    original_publish = controller._catalog.publish

    def paused_publish(candidate, *, revision=None):
        public_published.set()
        assert allow_private_publish.wait(timeout=2)
        return original_publish(candidate, revision=revision)

    controller._catalog.publish = paused_publish
    commit_thread = threading.Thread(target=controller._commit_catalog_build, args=(build,))
    commit_thread.start()
    assert public_published.wait(timeout=2)

    observed: list[tuple[int, str]] = []

    def read_published_catalog() -> None:
        revision = controller.state.catalog_revision
        resolved = controller._catalog.resolve_play(
            PlayCommand(
                str(uuid.uuid4()),
                ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1"),
                revision,
            )
        )
        observed.append((revision, resolved.current_item["url"]))
        reader_completed.set()

    reader_thread = threading.Thread(target=read_published_catalog)
    reader_thread.start()
    assert not reader_completed.wait(timeout=0.05)
    allow_private_publish.set()
    commit_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not commit_thread.is_alive()
    assert not reader_thread.is_alive()
    assert observed == [(1, str(media_path))]
    controller.stop()


def test_language_change_publishes_profile_before_localized_catalog(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])
    events: list[str] = []
    controller._server = SimpleNamespace(
        publish_profile=lambda: events.append("profile"),
        publish_snapshot=lambda: events.append("catalog"),
    )
    localization = {
        "locale": "pt-BR",
        "messages": {"app.remoteControl": "Controle remoto"},
    }

    with patch(
        "solin.controllers.remote_control_controller.remote_control_localization",
        return_value=localization,
    ) as build_localization:
        controller.on_language_changed("pt_BR")

    build_localization.assert_called_once_with("pt_BR")
    assert controller._localization_payload() == localization
    assert events == ["profile", "catalog"]
    controller._server = None
    controller.stop()


def test_playback_origins_must_resolve_against_the_public_catalog(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])
    controller.state.update_catalog(
        (
            CatalogCollection(
                "linked-1",
                CatalogKind.LINKED_FOLDER,
                "Linked",
                nodes=(
                    CatalogNode(
                        "media-1",
                        CatalogNodeKind.MEDIA,
                        "Opening",
                    ),
                ),
            ),
        )
    )

    public_origin = controller._origin_from_item(
        {
            "origin_kind": "linked_folder",
            "origin_container_id": "linked-1",
            "origin_item_id": "media-1",
        },
        0,
    )
    private_origin = controller._origin_from_item(
        {
            "origin_kind": "linked_folder",
            "origin_container_id": r"C:\Private\Meeting media",
            "origin_item_id": "media-1",
        },
        0,
    )

    assert public_origin == ProjectionOrigin(
        ProjectionSource.LINKED_FOLDER,
        "linked-1",
        "media-1",
    )
    assert private_origin == ProjectionOrigin(
        ProjectionSource.TEMPORARY,
        node_id="queue-0",
    )
    assert "Private" not in str(private_origin.to_dict())
    controller.stop()


def test_runtime_reconciliation_recovers_and_fails_closed_with_the_interface(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, *_ = _controller(tmp_path, [])
    settings = controller._dependencies.settings
    settings.enabled = lambda: True
    settings.network_selection = lambda: object()
    controller._dependencies.credentials.has_credentials = lambda: True
    reconciliations: list[str] = []
    monkeypatch.setattr(controller, "reconfigure", lambda: reconciliations.append("run"))

    monkeypatch.setattr(
        controller,
        "_resolve_selected_interface",
        lambda _selection: object(),
    )
    controller._reconcile_runtime()

    controller._server = SimpleNamespace(is_running=True)
    monkeypatch.setattr(
        controller,
        "_resolve_selected_interface",
        lambda _selection: None,
    )
    controller._reconcile_runtime()

    assert reconciliations == ["run", "run"]
    controller._server = None
    controller.stop()


def test_play_resolves_private_media_and_annotates_the_projection_queue(
    tmp_path: Path,
) -> None:
    media_path = tmp_path / "opening.mp4"
    media_path.write_bytes(b"media")
    playlist = {
        "id": "playlist-1",
        "name": "Program",
        "items": [
            {
                "id": "media-1",
                "title": "Opening",
                "type": "video",
                "url": str(media_path),
            }
        ],
    }
    controller, *_rest, media_projection = _controller(tmp_path, [playlist])
    controller._refresh_catalog()
    command = PlayCommand(
        str(uuid.uuid4()),
        ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1"),
        controller.state.catalog_revision,
    )

    error = controller._execute_command(command)

    assert error is None
    assert media_projection.projected is not None
    queue, index = media_projection.projected
    assert index == 0
    assert queue[0]["url"] == str(media_path)
    assert queue[0]["origin_kind"] == "playlist"
    controller.stop()


def test_play_uses_the_exact_private_catalog_published_to_remote_clients(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    playlist = {
        "id": "playlist-1",
        "name": "Program",
        "items": [
            {
                "id": "media-1",
                "title": "Opening",
                "type": "video",
                "url": str(first_path),
            }
        ],
    }
    controller, *_rest, media_projection = _controller(tmp_path, [playlist])
    controller._refresh_catalog()
    published_revision = controller.state.catalog_revision
    playlist["items"][0]["url"] = str(second_path)
    command = PlayCommand(
        str(uuid.uuid4()),
        ProjectionOrigin(ProjectionSource.PLAYLIST, "playlist-1", "media-1"),
        published_revision,
    )

    error = controller._execute_command(command)

    assert error is None
    assert media_projection.projected is not None
    assert media_projection.projected[0][0]["url"] == str(first_path)
    assert controller.state.catalog_revision == published_revision

    controller._refresh_catalog()
    assert controller.state.catalog_revision == published_revision + 1
    stale_error = controller._execute_command(command)
    assert stale_error is not None
    assert stale_error.code is CommandErrorCode.CATALOG_STALE

    refreshed_command = PlayCommand(
        str(uuid.uuid4()),
        command.origin,
        controller.state.catalog_revision,
    )
    assert controller._execute_command(refreshed_command) is None
    assert media_projection.projected[0][0]["url"] == str(second_path)
    controller.stop()


def test_playback_snapshot_mirrors_desktop_queue_capabilities_and_position(
    tmp_path: Path,
) -> None:
    controller, session, bar, media, _projection = _controller(tmp_path, [])
    controller.state.update_catalog(
        (
            CatalogCollection(
                "playlist-1",
                CatalogKind.PLAYLIST,
                "Program",
                nodes=(
                    CatalogNode(
                        "media-1",
                        CatalogNodeKind.MEDIA,
                        "Opening",
                        thumbnail_id="thumb-media-1",
                    ),
                ),
            ),
        )
    )
    session.state_type = "video"
    session.state = {
        "type": "video",
        "title": "Opening",
        "is_audio": False,
        "origin": {
            "kind": "playlist",
            "container_id": "playlist-1",
            "item_id": "media-1",
        },
    }
    session.session_id = 7
    media.session_id = 9
    media.duration = 120_000
    media.position = 23_000
    media.player.state = QMediaPlayer.PlaybackState.PlayingState
    bar.items = [
        {
            "id": "media-1",
            "title": "Opening",
            "type": "video",
            "origin_kind": "playlist",
            "origin_container_id": "playlist-1",
            "origin_item_id": "media-1",
        }
    ]

    controller._refresh_playback()
    playback = controller.state.playback

    assert playback.state is PlaybackState.PLAYING
    assert playback.playback_session_id == "projection-7-media-9"
    assert playback.media_kind is RemoteMediaKind.VIDEO
    assert playback.position_ms == 23_000
    assert playback.duration_ms == 120_000
    assert playback.volume == 72
    assert playback.capabilities.can_pause is True
    assert playback.queue[0].origin == ProjectionOrigin(
        ProjectionSource.PLAYLIST,
        "playlist-1",
        "media-1",
    )
    assert playback.queue[0].thumbnail_id == "thumb-media-1"
    controller.stop()


def test_persisted_meeting_cover_is_exposed_as_normalized_collection_thumbnail(
    tmp_path: Path,
) -> None:
    controller, *_ = _controller(tmp_path, [])
    cover = BytesIO()
    Image.new("RGB", (400, 600), (32, 104, 176)).save(cover, "PNG")
    controller._dependencies.meeting_tree_store.save(
        "mwb:2026-07-13:E:1",
        [],
        "canonical",
        overview=MeetingTreeOverview(
            title="Life and Ministry",
            cover_bytes=cover.getvalue(),
        ),
    )
    controller._refresh_catalog()

    collection = controller.state.catalog.collections[0]
    data = asyncio.run(controller._load_collection_thumbnail("meeting", collection.id))

    assert collection.thumbnail_id is not None
    assert data is not None and data.startswith(b"\xff\xd8")
    assert asyncio.run(controller._load_collection_thumbnail("playlist", collection.id)) is None
    controller.stop()


def test_resolved_meeting_artwork_is_generated_on_first_remote_request(
    tmp_path: Path,
) -> None:
    controller, *_ = _controller(tmp_path, [])
    thumbnail_url = "https://media.example.test/private/song-thumb.jpg?token=secret"
    video_url = "https://media.example.test/private/song.mp4?token=secret"
    controller._dependencies.meeting_tree_store.save(
        "mwb:2026-07-13:T:20260700",
        [
            {
                "id": "song-34",
                "type": "media",
                "title": "34. Andarei em integridade",
                "media_type": "video",
                "resolved_url": video_url,
                "thumbnail_url": thumbnail_url,
                "media_ref": {
                    "mime_type": "video/mp4",
                    "key_symbol": "sjjm",
                    "track": 34,
                },
                "children": [],
            }
        ],
        "canonical",
        overview=MeetingTreeOverview(title="Life and Ministry"),
    )
    output = BytesIO()
    Image.new("RGB", (640, 360), (24, 112, 196)).save(output, "JPEG")
    extractor = controller._dependencies.media_thumbnail_extractor
    extractor.results[MediaKind.IMAGE] = output.getvalue()
    controller._refresh_catalog()
    collection = controller.state.catalog.collections[0]
    node = collection.nodes[0]

    data = asyncio.run(controller._load_thumbnail("meeting", collection.id, node.id))

    assert node.thumbnail_id is not None
    assert data is not None and data.startswith(b"\xff\xd8")
    assert extractor.requests == [(thumbnail_url, MediaKind.IMAGE)]
    assert controller._dependencies.meeting_thumbnail_store.exists(node.thumbnail_id)
    public_catalog = json.dumps(controller.state.catalog.to_dict())
    assert thumbnail_url not in public_catalog
    assert video_url not in public_catalog
    controller.stop()


def test_failed_meeting_artwork_keeps_the_original_video_placeholder(
    tmp_path: Path,
) -> None:
    controller, *_ = _controller(tmp_path, [])
    thumbnail_url = "https://media.example.test/private/video-thumb.jpg"
    controller._dependencies.meeting_tree_store.save(
        "mwb:2026-07-13:T:20260700",
        [
            {
                "id": "video-135",
                "type": "media",
                "title": "135. Seja sábio, meu filho",
                "media_type": "video",
                "thumbnail_url": thumbnail_url,
                "media_ref": {"mime_type": "video/mp4", "key_symbol": "sjjm"},
                "children": [],
            }
        ],
        "canonical",
        overview=MeetingTreeOverview(title="Life and Ministry"),
    )
    controller._refresh_catalog()
    collection = controller.state.catalog.collections[0]
    node = collection.nodes[0]

    data = asyncio.run(controller._load_thumbnail("meeting", collection.id, node.id))

    assert controller._dependencies.media_thumbnail_extractor.requests == [
        (thumbnail_url, MediaKind.IMAGE)
    ]
    assert data == render_media_placeholder_thumbnail(MediaKind.VIDEO)
    assert not controller._dependencies.meeting_thumbnail_store.exists(node.thumbnail_id)
    controller.stop()


def test_session_command_is_revalidated_on_main_thread_before_effect(
    tmp_path: Path,
) -> None:
    controller, session, _bar, media, _projection = _controller(tmp_path, [])
    session.state_type = "video"
    session.state = {"type": "video", "title": "First media"}
    session.session_id = 1
    media.session_id = 1
    media.player.state = QMediaPlayer.PlaybackState.PlayingState
    controller._refresh_playback()
    old_session_id = controller.state.playback.playback_session_id
    assert old_session_id is not None
    command = PauseCommand(str(uuid.uuid4()), old_session_id)
    decision = ProjectionCommandSession(controller.state).prepare(command)
    assert decision.should_execute is True

    session.state = {"type": "video", "title": "Replacement media"}
    session.session_id = 2
    media.session_id = 2
    controller._refresh_playback()
    error = controller._execute_command(command)

    assert error is not None
    assert error.code is CommandErrorCode.PLAYBACK_STALE
    assert media.player.state is QMediaPlayer.PlaybackState.PlayingState
    controller.stop()


def test_playback_snapshot_sanitizes_private_titles_queue_and_errors(
    tmp_path: Path,
) -> None:
    controller, session, bar, media, _projection = _controller(tmp_path, [])
    session.state_type = "video"
    session.state = {
        "type": "video",
        "title": r"C:\Users\Alex\private\meeting.mp4",
    }
    session.session_id = 4
    media.session_id = 8
    bar.items = [
        {
            "id": r"C:\Private\media-1",
            "title": "https://private.invalid/media.mp4",
            "type": "video",
        }
    ]
    controller._on_media_error(r"Could not open C:\Users\Alex\secret.mp4")
    playback = controller.state.playback
    encoded = json.dumps(playback.to_dict())

    assert playback.title == "Media"
    assert playback.queue[0].title == "Media"
    assert playback.error is not None
    assert playback.error.message == "The media could not be played."
    assert "Users" not in encoded
    assert "Private" not in encoded
    assert "private.invalid" not in encoded
    controller.stop()


def test_failed_server_stop_keeps_lifecycle_ownership(tmp_path: Path) -> None:
    controller, *_ = _controller(tmp_path, [])

    class _Server:
        is_running = True

        def stop(self) -> None:
            raise TimeoutError

    server = _Server()
    identity = object()
    controller._server = server
    controller._tls_identity = identity

    assert controller._stop_server() is False
    assert controller._server is server
    assert controller._tls_identity is identity

    controller._server = None
    controller._tls_identity = None
    controller.stop()


def test_tls_renewal_reloads_live_context_without_revoking_sessions_or_stopping_server(
    tmp_path: Path,
) -> None:
    store = TLSCertificateStore(tmp_path / "tls")
    previous = store.load_or_create("192.168.1.20")
    renewal_store = TLSCertificateStore(
        tmp_path / "tls",
        clock=lambda: previous.renew_at + timedelta(seconds=1),
    )
    renewed = renewal_store.load_or_create("192.168.1.20")
    controller, *_ = _controller(tmp_path, [])

    class _Server:
        is_running = True
        binding = SimpleNamespace(url="https://192.168.1.20:8765/remote/")

        def __init__(self) -> None:
            self.reloaded = []
            self.stop_called = False

        def reload_tls(self, identity) -> None:
            self.reloaded.append(identity)

        def stop(self) -> None:
            self.stop_called = True

    server = _Server()
    controller._server = server
    controller._tls_identity = previous
    controller._tls_store = SimpleNamespace(
        load_or_create=lambda _address: renewed,
    )

    controller._renew_tls()

    assert server.reloaded == [renewed]
    assert server.stop_called is False
    assert controller._server is server
    assert controller._tls_identity is renewed
    assert controller._sessions.generation == 0

    controller._server = None
    controller._tls_identity = None
    controller.stop()


def test_failed_tls_hot_reload_keeps_running_identity_and_backs_off(tmp_path: Path) -> None:
    store = TLSCertificateStore(tmp_path / "tls")
    previous = store.load_or_create("192.168.1.20")
    renewal_store = TLSCertificateStore(
        tmp_path / "tls",
        clock=lambda: previous.renew_at + timedelta(seconds=1),
    )
    renewed = renewal_store.load_or_create("192.168.1.20")
    controller, *_ = _controller(tmp_path, [])

    class _Server:
        is_running = True
        binding = SimpleNamespace(url="https://192.168.1.20:8765/remote/")

        @staticmethod
        def reload_tls(_identity) -> None:
            raise RuntimeError("synthetic reload failure")

    server = _Server()
    controller._server = server
    controller._tls_identity = previous
    controller._tls_store = SimpleNamespace(
        load_or_create=lambda _address: renewed,
    )

    before = time.monotonic()
    controller._renew_tls()

    assert controller._server is server
    assert controller._tls_identity is previous
    assert controller._next_tls_renewal_attempt_at >= before + 299

    controller._server = None
    controller._tls_identity = None
    controller.stop()


def test_local_images_get_safe_on_demand_remote_thumbnails(tmp_path: Path) -> None:
    source = tmp_path / "private" / "photo.png"
    source.parent.mkdir()
    Image.new("RGBA", (1_200, 800), (34, 139, 230, 180)).save(source)
    playlist = {
        "id": "playlist-1",
        "name": "Images",
        "items": [
            {
                "id": "image-1",
                "title": "Photo",
                "type": "image",
                "url": str(source),
            }
        ],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    controller._refresh_catalog()
    node = controller.state.catalog.collections[0].nodes[0]

    data = asyncio.run(controller._load_thumbnail("playlist", "playlist-1", "image-1"))

    storage_id = thumbnail_storage_id("image-1", str(source))
    assert node.thumbnail_id == storage_id
    assert data is not None and data.startswith(b"\xff\xd8")
    assert controller._dependencies.playlist_thumbnail_store.exists(storage_id)
    assert str(source) not in json.dumps(controller.state.catalog.to_dict())
    controller.stop()


def test_local_video_and_audio_thumbnails_are_generated_and_cached_on_demand(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    video = private / "clip.mp4"
    audio = private / "song.mp3"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    playlist = {
        "id": "playlist-1",
        "name": "Media",
        "items": [
            {"id": "video-1", "title": "Clip", "type": "video", "url": str(video)},
            {"id": "audio-1", "title": "Song", "type": "audio", "url": str(audio)},
        ],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    extractor = controller._dependencies.media_thumbnail_extractor
    output = BytesIO()
    Image.new("RGB", (800, 450), (18, 110, 210)).save(output, "JPEG")
    extractor.results[MediaKind.VIDEO] = output.getvalue()
    controller._refresh_catalog()

    async def load() -> tuple[bytes | None, bytes | None]:
        return await asyncio.gather(
            controller._load_thumbnail("playlist", "playlist-1", "video-1"),
            controller._load_thumbnail("playlist", "playlist-1", "audio-1"),
        )

    video_data, audio_data = asyncio.run(load())
    nodes = controller.state.catalog.collections[0].nodes

    video_storage_id = thumbnail_storage_id("video-1", str(video))
    audio_storage_id = thumbnail_storage_id("audio-1", str(audio))
    assert [node.thumbnail_id for node in nodes] == [
        video_storage_id,
        audio_storage_id,
    ]
    assert video_data is not None and video_data.startswith(b"\xff\xd8")
    assert audio_data is not None and audio_data.startswith(b"\xff\xd8")
    assert extractor.requests == [
        (str(video), MediaKind.VIDEO),
        (str(audio), MediaKind.AUDIO),
    ]
    assert controller._dependencies.playlist_thumbnail_store.exists(video_storage_id)
    assert not controller._dependencies.playlist_thumbnail_store.exists(audio_storage_id)
    assert (
        asyncio.run(controller._load_thumbnail("playlist", "playlist-1", "audio-1")) == audio_data
    )
    assert len(extractor.requests) == 2
    assert str(video) not in json.dumps(controller.state.catalog.to_dict())
    assert str(audio) not in json.dumps(controller.state.catalog.to_dict())
    controller.stop()


def test_concurrent_thumbnail_requests_share_one_media_extraction(tmp_path: Path) -> None:
    source = tmp_path / "private" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    playlist = {
        "id": "playlist-1",
        "name": "Media",
        "items": [{"id": "video-1", "title": "Clip", "type": "video", "url": str(source)}],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    extractor = controller._dependencies.media_thumbnail_extractor
    extractor.delay = 0.01
    controller._refresh_catalog()

    async def load() -> tuple[bytes | None, bytes | None]:
        return await asyncio.gather(
            controller._load_thumbnail("playlist", "playlist-1", "video-1"),
            controller._load_thumbnail("playlist", "playlist-1", "video-1"),
        )

    first, second = asyncio.run(load())

    assert first == second
    assert first is not None and first.startswith(b"\xff\xd8")
    assert extractor.requests == [(str(source), MediaKind.VIDEO)]
    controller.stop()


def test_cancelled_thumbnail_waiter_does_not_leak_completed_singleflight_task(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    playlist = {
        "id": "playlist-1",
        "name": "Media",
        "items": [{"id": "video-1", "title": "Clip", "type": "video", "url": str(source)}],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    controller._dependencies.media_thumbnail_extractor.delay = 0.01
    controller._refresh_catalog()

    async def cancel_waiter() -> None:
        pending = asyncio.create_task(
            controller._load_thumbnail("playlist", "playlist-1", "video-1")
        )
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.03)

    asyncio.run(cancel_waiter())

    assert controller._thumbnail_tasks == {}
    controller.stop()


def test_thumbnail_generation_does_not_cross_catalog_revision(tmp_path: Path) -> None:
    first = tmp_path / "private" / "first.mp4"
    second = tmp_path / "private" / "second.mp4"
    first.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    playlist = {
        "id": "playlist-1",
        "name": "Media",
        "items": [{"id": "video-1", "title": "Clip", "type": "video", "url": str(first)}],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    extractor = controller._dependencies.media_thumbnail_extractor
    output = BytesIO()
    Image.new("RGB", (320, 180), (18, 110, 210)).save(output, "JPEG")
    extractor.results[MediaKind.VIDEO] = output.getvalue()
    extractor.delay = 0.01
    controller._refresh_catalog()

    async def change_catalog_while_loading() -> bytes | None:
        pending = asyncio.create_task(
            controller._load_thumbnail("playlist", "playlist-1", "video-1")
        )
        await asyncio.sleep(0)
        playlist["items"][0]["url"] = str(second)
        controller._refresh_catalog()
        return await pending

    assert asyncio.run(change_catalog_while_loading()) is None
    assert not controller._dependencies.playlist_thumbnail_store.exists("video-1")
    controller.stop()


def test_stored_remote_media_thumbnail_is_resolved_by_catalog_id_only(tmp_path: Path) -> None:
    private_url = "https://media.example.test/private/clip.mp4?token=secret"
    playlist = {
        "id": "playlist-1",
        "name": "Media",
        "items": [{"id": "video-1", "title": "Clip", "type": "video", "url": private_url}],
    }
    controller, *_ = _controller(tmp_path, [playlist])
    controller._refresh_catalog()
    node = controller.state.catalog.collections[0].nodes[0]

    data = asyncio.run(controller._load_thumbnail("playlist", "playlist-1", "video-1"))

    storage_id = thumbnail_storage_id("video-1", private_url)
    assert node.thumbnail_id == storage_id
    assert data is not None and data.startswith(b"\xff\xd8")
    assert controller._dependencies.media_thumbnail_extractor.requests == [
        (private_url, MediaKind.VIDEO)
    ]
    assert private_url not in json.dumps(controller.state.catalog.to_dict())
    controller.stop()
