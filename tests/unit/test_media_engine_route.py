"""App-side Fork A cutover: MediaController routes local files through the engine."""
from __future__ import annotations

from concurrent.futures import Future

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QImage, QPixmap

from solin.controllers.media_engine_route import SceneEngineMediaRoute
from solin.core.media.cache import MediaCacheManager
from solin.core.media.playback import MediaController
from solin.core.media.playback_request import MediaPlaybackRequest, MediaTrim
from solin.core.scenes.media_control import (
    MediaControlAction,
    MediaPlaybackNativeState,
    MediaPlaybackState,
)


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


class _MediaSettings:
    def auto_download_on_play(self) -> bool:
        return False


class _Downloader(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    error = Signal(str)

    def get_cached_path(self, _url):
        return None

    def start(self, url, persist=True):
        pass

    def cancel(self):
        pass

    def cleanup_temp(self):
        pass


class _FakeRoute:
    def __init__(self) -> None:
        self.calls: list = []

    def open(self, path, **kwargs):
        self.calls.append(("open", path, kwargs))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def restart(self):
        self.calls.append(("restart",))

    def seek(self, ms):
        self.calls.append(("seek", ms))

    def close(self):
        self.calls.append(("close",))

    def set_properties(self, **kwargs):
        self.calls.append(("props", kwargs))


def _controller(tmp_path):
    _app()
    cache = MediaCacheManager(tmp_path, downloader_factory=lambda _p: _Downloader())
    controller = MediaController(
        _MediaSettings(), cache, downloader_factory=lambda _p: _Downloader()
    )
    played: list[str] = []
    controller._play_source = played.append  # intercept the Qt path
    return controller, played


def _tags(route: _FakeRoute) -> list[str]:
    return [call[0] for call in route.calls]


# ── routing decision ─────────────────────────────────────────────────────────


def test_local_file_routes_to_engine_and_not_qt(tmp_path):
    controller, played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    controller.start_playback(MediaPlaybackRequest("/videos/clip.mp4"))

    assert _tags(route) == ["open"]
    assert route.calls[0][1] == "/videos/clip.mp4"
    assert route.calls[0][2]["is_local_file"] is True
    assert played == []  # the Qt player was not used
    assert controller._engine_route_active is True


def test_remote_url_routes_and_streams(tmp_path):
    controller, played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)

    controller.start_playback(MediaPlaybackRequest("https://cdn.example/clip.mp4"))

    # Full libobs cutover: an uncached remote URL streams through the sidecar's
    # ffmpeg_source (is_local_file=False), not Qt.
    assert _tags(route) == ["open"]
    assert route.calls[0][1] == "https://cdn.example/clip.mp4"
    assert route.calls[0][2]["is_local_file"] is False
    assert played == []  # the Qt player was not used
    assert controller._engine_route_active is True


def test_without_a_route_emits_engine_unavailable(tmp_path):
    controller, played = _controller(tmp_path)
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    controller.start_playback(MediaPlaybackRequest("/videos/clip.mp4"))

    # libobs is the only engine; with no route there is nothing to decode, so the
    # failure is surfaced rather than silently played via Qt.
    assert errors == ["engine_unavailable"]
    assert played == []
    assert controller._engine_route_active is False


class _NotReadyRoute(_FakeRoute):
    def is_ready(self):
        return False


class _ReadyRoute(_FakeRoute):
    def is_ready(self):
        return True


def test_engine_not_ready_emits_engine_unavailable(tmp_path):
    controller, played = _controller(tmp_path)
    route = _NotReadyRoute()
    controller.set_engine_media_route(route)
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    controller.start_playback(MediaPlaybackRequest("/videos/clip.mp4"))

    # the sidecar is not ready → surface engine_unavailable, do not open the route
    assert errors == ["engine_unavailable"]
    assert route.calls == []
    assert played == []
    assert controller._engine_route_active is False


def test_engine_ready_routes(tmp_path):
    controller, played = _controller(tmp_path)
    route = _ReadyRoute()
    controller.set_engine_media_route(route)
    controller.start_playback(MediaPlaybackRequest("/videos/clip.mp4"))
    assert _tags(route) == ["open"] and played == []


def test_open_forwards_trim_offsets(tmp_path):
    controller, _played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    # 5000 ms trimmed from the start, 3000 ms from the end (JW 100ns ticks)
    trim = MediaTrim(
        start_trim_ticks=50_000_000, end_trim_ticks=30_000_000,
        base_duration_ticks=600_000_000,
    )
    controller.start_playback(MediaPlaybackRequest("/clip.mp4", trim=trim))

    kwargs = route.calls[0][2]
    assert kwargs["trim_start_ms"] == 5000
    assert kwargs["trim_end_ms"] == 3000
    assert kwargs["volume_percent"] == 100 and kwargs["speed_percent"] == 100


# ── transport delegation ─────────────────────────────────────────────────────


def test_transport_calls_delegate_to_route(tmp_path):
    controller, _played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    controller.start_playback(MediaPlaybackRequest("/clip.mp4"))
    route.calls.clear()

    controller.pause()
    controller.play()
    controller.seek(4200)
    controller.replay()
    controller.set_volume(0.5)
    controller.set_playback_rate(1.5)

    assert _tags(route) == ["pause", "play", "seek", "restart", "props", "props"]
    assert ("seek", 4200) in route.calls
    volume_props = [c for c in route.calls if c[0] == "props"][0][1]
    assert volume_props["volume_percent"] == 50
    speed_props = [c for c in route.calls if c[0] == "props"][1][1]
    assert speed_props["speed_percent"] == 150


def test_stop_closes_route_and_reverts_to_qt(tmp_path):
    controller, played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    controller.start_playback(MediaPlaybackRequest("/clip.mp4"))

    controller.stop()
    assert ("close",) in route.calls
    assert controller._engine_route_active is False

    # a subsequent local file routes again
    route.calls.clear()
    controller.start_playback(MediaPlaybackRequest("/clip2.mp4"))
    assert _tags(route) == ["open"]


def test_toggle_play_pause_uses_engine_state(tmp_path):
    controller, _played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    controller.start_playback(MediaPlaybackRequest("/clip.mp4"))
    route.calls.clear()

    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.PLAYING, 0, 1000))
    controller.toggle_play_pause()
    assert _tags(route) == ["pause"]

    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.PAUSED, 0, 1000))
    controller.toggle_play_pause()
    assert _tags(route) == ["pause", "play"]


# ── state feedback ───────────────────────────────────────────────────────────


def test_engine_state_drives_signals_and_properties(tmp_path):
    controller, _played = _controller(tmp_path)
    route = _FakeRoute()
    controller.set_engine_media_route(route)
    controller.start_playback(MediaPlaybackRequest("/clip.mp4"))

    positions: list[int] = []
    durations: list[int] = []
    ended: list[bool] = []
    controller.position_changed.connect(positions.append)
    controller.duration_changed.connect(durations.append)
    controller.media_ended.connect(lambda: ended.append(True))

    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.PLAYING, 1000, 5000))
    assert controller.is_playing is True and controller.is_paused is False
    assert controller.position == 1000 and controller.duration == 5000
    assert positions[-1] == 1000 and durations == [5000]

    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.PAUSED, 1200, 5000))
    assert controller.is_paused is True
    assert durations == [5000]  # unchanged duration not re-emitted

    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.ENDED, 5000, 5000))
    assert ended == [True]
    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.ENDED, 5000, 5000))
    assert ended == [True]  # media_ended fires once


def test_engine_state_ignored_when_not_routed(tmp_path):
    controller, _played = _controller(tmp_path)
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    # not routed → engine events are ignored
    controller.on_engine_media_state(MediaPlaybackNativeState(MediaPlaybackState.ENDED, 0, 0))
    assert ended == []


# ── SceneEngineMediaRoute adapter ────────────────────────────────────────────


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list = []

    def open_media(self, path, **kwargs):
        self.calls.append(("open_media", path, kwargs))
        return Future()

    def control_media(self, action, **kwargs):
        self.calls.append(("control_media", action, kwargs))
        return Future()

    def set_media_properties(self, **kwargs):
        self.calls.append(("set_media_properties", kwargs))
        return Future()


def test_scene_engine_route_translates_to_engine_calls():
    engine = _FakeEngine()
    route = SceneEngineMediaRoute(engine)

    route.open("/c.mp4", is_local_file=True, autoplay=True, volume_percent=80,
               speed_percent=100, trim_start_ms=0, trim_end_ms=0)
    route.play()
    route.seek(1500)
    route.close()
    route.set_properties(volume_percent=50, speed_percent=150)

    kinds = [c[0] for c in engine.calls]
    assert kinds == ["open_media", "control_media", "control_media",
                     "control_media", "set_media_properties"]
    assert engine.calls[0][2]["is_local_file"] is True and engine.calls[0][2]["autoplay"] is True
    assert engine.calls[1][1] is MediaControlAction.PLAY
    assert engine.calls[2][1] is MediaControlAction.SEEK
    assert engine.calls[2][2]["position_ms"] == 1500
    assert engine.calls[3][1] is MediaControlAction.CLOSE
    assert engine.calls[4][1]["volume_percent"] == 50


# ── routed metadata / cover-art ───────────────────────────────────────────────


class _FakeExtractor(QObject):
    metadata_ready = Signal(int, str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.requests: list = []
        self.cancels = 0

    def request(self, session_id: int, path: str) -> None:
        self.requests.append((session_id, path))

    def cancel(self) -> None:
        self.cancels += 1


def _controller_with_extractor(tmp_path):
    _app()
    cache = MediaCacheManager(tmp_path, downloader_factory=lambda _p: _Downloader())
    holder: dict = {}

    def factory(parent):
        holder["ex"] = _FakeExtractor(parent)
        return holder["ex"]

    controller = MediaController(
        _MediaSettings(), cache, downloader_factory=lambda _p: _Downloader(),
        metadata_extractor_factory=factory,
    )
    controller._play_source = lambda *_a, **_k: None  # intercept the Qt path
    return controller, holder["ex"]


def _pixmap() -> QPixmap:
    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFF3366CC)
    return QPixmap.fromImage(img)


def test_routed_playback_requests_metadata(tmp_path):
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.set_engine_media_route(_FakeRoute())
    controller.start_playback(MediaPlaybackRequest("/videos/clip.mp4"))
    assert extractor.requests == [(controller.session_id, "/videos/clip.mp4")]


def test_metadata_ready_emits_title_and_cover(tmp_path):
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.set_engine_media_route(_FakeRoute())
    controller.start_playback(MediaPlaybackRequest("/a.mp3"))

    titles: list[str] = []
    covers: list = []
    controller.title_from_metadata.connect(titles.append)
    controller.cover_art_changed.connect(covers.append)

    px = _pixmap()
    extractor.metadata_ready.emit(controller.session_id, "My Song", px)

    assert titles == ["My Song"]
    assert covers == [px]
    assert controller._session.cover_emitted is True


def test_metadata_ready_without_cover_emits_none(tmp_path):
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.set_engine_media_route(_FakeRoute())
    controller.start_playback(MediaPlaybackRequest("/a.mp4"))

    titles: list[str] = []
    covers: list = []
    controller.title_from_metadata.connect(titles.append)
    controller.cover_art_changed.connect(covers.append)

    extractor.metadata_ready.emit(controller.session_id, "", None)

    assert titles == []          # empty title is not emitted
    assert covers == [None]
    assert controller._session.cover_emitted is False


def test_stale_session_metadata_is_dropped(tmp_path):
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.set_engine_media_route(_FakeRoute())
    controller.start_playback(MediaPlaybackRequest("/a.mp3"))

    titles: list[str] = []
    covers: list = []
    controller.title_from_metadata.connect(titles.append)
    controller.cover_art_changed.connect(covers.append)

    # a result tagged with a superseded session id must not fire
    extractor.metadata_ready.emit(controller.session_id - 1, "Old", _pixmap())
    assert titles == [] and covers == []


def test_non_routed_playback_does_not_request_metadata(tmp_path):
    # With no engine route configured, playback falls back to Qt, which reads its
    # own tags — so the routed-metadata extractor is not invoked.
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/clip.mp4"))
    assert extractor.requests == []


def test_stop_cancels_metadata_extraction(tmp_path):
    controller, extractor = _controller_with_extractor(tmp_path)
    controller.set_engine_media_route(_FakeRoute())
    controller.start_playback(MediaPlaybackRequest("/a.mp3"))
    controller.stop()
    assert extractor.cancels >= 1
    # a late result after stop is dropped (route no longer active)
    titles: list[str] = []
    controller.title_from_metadata.connect(titles.append)
    extractor.metadata_ready.emit(controller.session_id, "Late", None)
    assert titles == []
