"""Cache / download-policy behaviour of the engine-routed MediaController.

libobs is the only media engine now: remote URLs stream through the sidecar's
``ffmpeg_source`` directly, an already-cached copy is preferred, and (per cache
policy) a background download populates the cache for offline reuse. The Qt
streaming / http->local source-swap / buffer-bar / reconnect state machine is
gone, so only the surviving cache decisions are exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.media.cache import MediaCacheManager
from solin.core.media.playback import MediaController
from solin.core.media.playback_request import (
    MediaPlaybackRequest,
    PlaybackCachePolicy,
)


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


class _MediaSettings:
    def __init__(self, auto_download: bool) -> None:
        self._auto_download = auto_download

    def auto_download_on_play(self) -> bool:
        return self._auto_download


class _Downloader(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, cached_path: str | None = None) -> None:
        super().__init__()
        self.started: list[tuple[str, bool]] = []
        self.cancel_count = 0
        self.cleanup_count = 0
        self._cached_path = cached_path

    def get_cached_path(self, _url: str):
        return self._cached_path

    def start(self, url: str, persist: bool = True) -> None:
        self.started.append((url, persist))

    def cancel(self) -> None:
        self.cancel_count += 1

    def cleanup_temp(self) -> None:
        self.cleanup_count += 1


class _FakeExtractor(QObject):
    """No-op stand-in so tests do not spawn the real ffprobe worker thread."""

    metadata_ready = Signal(int, str, object)

    def request(self, session_id: int, path: str) -> None:
        pass

    def cancel(self) -> None:
        pass


class _FakeRoute:
    """Captures the transport vocabulary the controller drives."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, dict]] = []
        self.calls: list[str] = []

    def open(self, path, **kwargs):
        self.opened.append((path, kwargs))
        self.calls.append("open")

    def play(self):
        self.calls.append("play")

    def pause(self):
        self.calls.append("pause")

    def stop(self):
        self.calls.append("stop")

    def restart(self):
        self.calls.append("restart")

    def seek(self, ms):
        self.calls.append("seek")

    def close(self):
        self.calls.append("close")

    def set_properties(self, **kwargs):
        self.calls.append("props")


def _controller_with_downloader(
    tmp_path, *, auto_download: bool, cached_path: str | None = None
):
    _app()
    downloader = _Downloader(cached_path=cached_path)
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: _Downloader(),
    )
    controller = MediaController(
        _MediaSettings(auto_download),
        cache_manager,
        downloader_factory=lambda _parent: downloader,
        metadata_extractor_factory=lambda parent: _FakeExtractor(parent),
    )
    return controller, downloader


def _start(controller, url: str, *, policy=PlaybackCachePolicy.PROFILE_DEFAULT) -> None:
    controller.start_playback(MediaPlaybackRequest(url, cache_policy=policy))


REMOTE = "https://cdn.example/song.mp3"


# ── background cache download (_maybe_cache_remote) ──────────────────────────


def test_profile_default_with_auto_download_caches_remote_in_background(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    _start(controller, REMOTE)

    # streamed through the sidecar directly (is_local_file=False)…
    assert route.opened[0][0] == REMOTE
    assert route.opened[0][1]["is_local_file"] is False
    # …while the cache is populated in the background for offline reuse.
    assert downloader.started == [(REMOTE, True)]
    controller.stop()


def test_playback_occurrence_identity_is_scoped_to_active_request(tmp_path):
    controller, _downloader = _controller_with_downloader(
        tmp_path, auto_download=False
    )
    controller.set_engine_media_route(_FakeRoute())

    controller.start_playback(
        MediaPlaybackRequest(
            "https://cdn.example/clip.mp4",
            occurrence_id="first",
            occurrence_container_id="playlist",
        )
    )
    assert controller.current_occurrence_id == "first"
    assert controller.current_occurrence_container_id == "playlist"

    controller.stop()
    assert controller.current_occurrence_id == ""
    assert controller.current_occurrence_container_id == ""


def test_stopping_rejected_playback_clears_occurrence_identity(tmp_path):
    controller, _downloader = _controller_with_downloader(tmp_path, auto_download=False)
    controller.start_playback(MediaPlaybackRequest(
        REMOTE, occurrence_id="first", occurrence_container_id="playlist",
    ))

    controller.stop()

    assert controller.current_occurrence_id == ""
    assert controller.current_occurrence_container_id == ""
    assert controller.current_url == ""


def test_profile_default_without_auto_download_streams_without_caching(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=False)
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    _start(controller, REMOTE)

    assert route.opened[0][1]["is_local_file"] is False
    assert downloader.started == []
    controller.stop()


def test_persistent_policy_caches_regardless_of_setting(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=False)
    controller.set_engine_media_route(_FakeRoute())

    _start(controller, REMOTE, policy=PlaybackCachePolicy.PERSISTENT)

    assert downloader.started == [(REMOTE, True)]
    controller.stop()


def test_temporary_policy_streams_without_background_cache(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    _start(controller, REMOTE, policy=PlaybackCachePolicy.TEMPORARY)

    # the sidecar streams the remote directly; a temporary policy does not spill
    # a background copy to disk.
    assert route.opened[0][1]["is_local_file"] is False
    assert downloader.started == []
    controller.stop()


# ── prefer-cached-path ───────────────────────────────────────────────────────


def test_cached_remote_prefers_local_copy(tmp_path):
    cached = str(tmp_path / "cached-song.mp3")
    controller, downloader = _controller_with_downloader(
        tmp_path, auto_download=True, cached_path=cached
    )
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    _start(controller, REMOTE)

    # an already-cached copy is played as a local file, not re-streamed…
    assert route.opened[0][0] == cached
    assert route.opened[0][1]["is_local_file"] is True
    # …and no background download is kicked off.
    assert downloader.started == []
    assert downloader.cancel_count >= 1
    controller.stop()


def test_local_file_routes_local_and_never_downloads(tmp_path):
    local_media = tmp_path / "local-video.mp4"
    local_media.write_bytes(b"local")
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    source_states: list[bool] = []
    controller.playback_source_changed.connect(source_states.append)

    _start(controller, str(local_media))

    assert route.opened[0][0] == str(local_media)
    assert route.opened[0][1]["is_local_file"] is True
    assert downloader.started == []
    assert downloader.cancel_count >= 1
    assert source_states == [True]
    assert controller.current_url == str(local_media)
    assert controller.local_path == str(local_media)
    controller.stop()


# ── no engine available ──────────────────────────────────────────────────────


def test_without_a_route_emits_engine_unavailable_and_skips_caching(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    _start(controller, REMOTE)

    # libobs is the only engine; with no route there is nothing to decode, so
    # nothing is streamed or cached.
    assert errors == ["engine_unavailable"]
    assert downloader.started == []


# ── the "playing offline" badge must follow the actual source ────────────────


def test_streamed_media_does_not_claim_to_be_playing_offline(tmp_path):
    """The badge means "playing from a local copy".

    It was emitted unconditionally whenever engine playback began, so a streamed
    item lit it too — telling the operator a network-dependent item was safe to
    run with no connection.
    """
    controller, _downloader = _controller_with_downloader(tmp_path, auto_download=True)
    controller.set_engine_media_route(_FakeRoute())
    source_states: list[bool] = []
    controller.playback_source_changed.connect(source_states.append)

    _start(controller, REMOTE)

    assert source_states == [False]
    controller.stop()


def test_a_cached_copy_still_reports_offline(tmp_path):
    """A remote item already on disk plays locally, so the badge belongs."""
    cached = tmp_path / "cached-song.mp3"
    cached.write_bytes(b"cached")
    controller, _downloader = _controller_with_downloader(
        tmp_path, auto_download=True, cached_path=str(cached)
    )
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    source_states: list[bool] = []
    controller.playback_source_changed.connect(source_states.append)

    _start(controller, REMOTE)

    assert route.opened[0][1]["is_local_file"] is True
    assert source_states == [True]
    controller.stop()


# ── the buffer bar behind the playback position ──────────────────────────────


def test_cache_progress_is_reported_while_a_stream_plays(tmp_path):
    """The transport draws how much is already on disk behind the position.

    The handler for this survived the QtMultimedia removal but its signal did
    not, so the buffer bar had nothing driving it.
    """
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    controller.set_engine_media_route(_FakeRoute())
    reported: list[tuple[int, int]] = []
    controller.buffer_progress.connect(lambda done, total: reported.append((done, total)))

    _start(controller, REMOTE)
    downloader.progress.emit(512, 2048)

    assert reported == [(512, 2048)]
    controller.stop()


def test_no_buffer_is_reported_for_a_local_file(tmp_path):
    """Nothing is downloading behind it, so a bar there would never move."""
    local_media = tmp_path / "local.mp4"
    local_media.write_bytes(b"local")
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    controller.set_engine_media_route(_FakeRoute())
    reported: list[tuple[int, int]] = []
    controller.buffer_progress.connect(lambda done, total: reported.append((done, total)))

    _start(controller, str(local_media))
    downloader.progress.emit(512, 2048)

    assert reported == []
    controller.stop()


def test_stopping_playback_stops_reporting_buffer(tmp_path):
    controller, downloader = _controller_with_downloader(tmp_path, auto_download=True)
    controller.set_engine_media_route(_FakeRoute())
    _start(controller, REMOTE)
    reported: list[tuple[int, int]] = []
    controller.buffer_progress.connect(lambda done, total: reported.append((done, total)))

    controller.stop()
    downloader.progress.emit(512, 2048)

    assert reported == []


def test_a_dropped_stream_reports_recovering_not_stopped(tmp_path):
    """The engine reports a dropped read as buffering; say so in the transport.

    The reconnecting slider animation lost its emitter with QtMultimedia, so a
    stream re-establishing itself just looked frozen.
    """
    from solin.core.media.playback_state import (
        ENGINE_STATE_BUFFERING,
        ENGINE_STATE_PLAYING,
    )

    controller, _downloader = _controller_with_downloader(tmp_path, auto_download=True)
    controller.set_engine_media_route(_FakeRoute())
    _start(controller, REMOTE)
    recovering: list[bool] = []
    controller.playback_recovery_changed.connect(recovering.append)

    controller.on_engine_media_state(
        SimpleNamespace(state=ENGINE_STATE_BUFFERING, position_ms=30_000,
                        duration_ms=140_000, path=REMOTE, error_code="")
    )
    QCoreApplication.processEvents()
    controller.on_engine_media_state(
        SimpleNamespace(state=ENGINE_STATE_PLAYING, position_ms=30_100,
                        duration_ms=140_000, path=REMOTE, error_code="")
    )
    QCoreApplication.processEvents()

    assert recovering == [True, False]
    controller.stop()
