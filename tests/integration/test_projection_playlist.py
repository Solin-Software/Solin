import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.playlist import ProjectionPlaylistMixin


class _Signal:
    def __init__(self):
        self.emissions = []

    def emit(self, *args):
        self.emissions.append(args)


class _Panel:
    def is_open(self):
        return False


class _Timer:
    def stop(self):
        pass


class _ThumbnailQueue:
    def clear(self):
        pass


class _PlaybackSettings:
    def playback_order(self):
        return "off"


def test_projection_bar_uses_playlist_mixin():
    assert projection_bar.ProjectionPlaylistMixin is ProjectionPlaylistMixin
    assert issubclass(projection_bar.ProjectionBar, ProjectionPlaylistMixin)
    assert projection_bar.ProjectionBar.set_playlist is ProjectionPlaylistMixin.set_playlist
    assert (
        projection_bar.ProjectionBar._toggle_playlist_panel
        is ProjectionPlaylistMixin._toggle_playlist_panel
    )
    assert (
        projection_bar.ProjectionBar._resolve_idle_path
        is ProjectionPlaylistMixin._resolve_idle_path
    )


def test_set_playlist_materializes_overlay_before_using_playlist_controls():
    calls = []
    host = type("ProjectionHost", (), {})()
    host._ensure_overlay_ready = lambda: calls.append("ready")
    host._playback_settings = _PlaybackSettings()
    host._live_thumb_timer = _Timer()
    host._thumb_queue = _ThumbnailQueue()
    host.playlist_panel = _Panel()
    host._update_nav_buttons = lambda: calls.append("navigation")

    ProjectionPlaylistMixin.set_playlist(host, [{"url": "video.mp4"}])

    assert calls == ["ready", "navigation"]


def test_source_duration_is_published_without_reaching_into_playlist_widgets():
    signal = _Signal()
    host = type("ProjectionHost", (), {})()
    host.current_playlist_item = lambda: {"id": "media-1"}
    host.source_duration_discovered = signal

    projection_bar.ProjectionBar._on_source_duration_changed(host, 12_345)

    assert signal.emissions == [("media-1", 12_345)]


def test_source_duration_ignores_missing_items_and_invalid_values():
    signal = _Signal()
    host = type("ProjectionHost", (), {})()
    host.current_playlist_item = lambda: None
    host.source_duration_discovered = signal

    projection_bar.ProjectionBar._on_source_duration_changed(host, 12_345)
    projection_bar.ProjectionBar._on_source_duration_changed(host, 0)

    assert signal.emissions == []


def test_preview_idle_path_accepts_only_existing_local_visual_media(tmp_path):
    video = tmp_path / "idle.mp4"
    video.write_bytes(b"video")
    host = type("ProjectionHost", (), {})()
    host._is_audio = False
    host._is_live_tab = False
    host._mode = "video"
    host._playlist_index = 0
    host._playlist = [{"url": str(video)}]

    assert ProjectionPlaylistMixin._resolve_idle_path(host) == str(video)

    host._playlist = [{"url": "https://example.test/idle.mp4"}]
    assert ProjectionPlaylistMixin._resolve_idle_path(host) == ""

    host._playlist = [{"url": str(video)}]
    host._is_audio = True
    assert ProjectionPlaylistMixin._resolve_idle_path(host) == ""


from types import SimpleNamespace

import pytest
from PySide6.QtGui import QPixmap


class _CaptureQueue:
    def __init__(self):
        self.requests = []
        self.covers = []
        self.has_artwork = False

    def has_real_thumb(self, _index):
        return self.has_artwork

    def request(self, *args):
        self.requests.append(args)

    def feed_live_cover(self, *args):
        self.covers.append(args)
        return True


class _CaptureHost(ProjectionPlaylistMixin):
    def __init__(self):
        self._live_thumb_captured = False
        self._mode = "video"
        self._playlist_index = 0
        self._playlist = [{"url": "clip.mp4", "type": "video"}]
        self._thumbnail_sources = {}
        self._thumb_queue = _CaptureQueue()
        self._is_audio = False
        self._audio_cover_pixmap = None
        self.media = SimpleNamespace(local_path="clip.mp4")


def test_live_thumbnail_fallback_uses_queue_once_for_current_source():
    host = _CaptureHost()

    host._do_live_thumb_capture()
    host._do_live_thumb_capture()

    assert host._thumb_queue.requests == [(0, "clip.mp4", "video")]
    assert not host._live_thumb_captured


def test_live_thumbnail_does_not_repeat_previous_queue_extraction():
    host = _CaptureHost()
    host._request_thumbnail(0, host._playlist[0])
    # The first attempt may have failed. Its retry remains the queue's decision.
    host._do_live_thumb_capture()
    host._request_thumbnail(0, host._playlist[0])
    assert host._thumb_queue.requests == [(0, "clip.mp4", "video")]


def test_live_thumbnail_keeps_http_source_for_completed_cache_fallback():
    host = _CaptureHost()
    host._playlist[0]["url"] = "https://example.test/clip.mp4"
    host.media.local_path = "cache/completed.mp4"
    host._do_live_thumb_capture()
    assert host._thumb_queue.requests == [(0, "https://example.test/clip.mp4", "video")]


def test_live_audio_thumbnail_sources_cover_only_audio_intent():
    host = _CaptureHost()
    host._is_audio = True
    host._do_live_thumb_capture()
    assert host._thumb_queue.requests == [(0, "clip.mp4", "audio")]


def test_live_thumbnail_preserves_existing_artwork():
    host = _CaptureHost()
    host._thumb_queue.has_artwork = True
    host._do_live_thumb_capture()
    assert host._live_thumb_captured and host._thumb_queue.requests == []


@pytest.mark.parametrize("path", ["", "https://example.test/a.mp4", "rtsp://camera", "rtmp://live"])
def test_live_thumbnail_does_not_extract_remote_playback_path(path):
    host = _CaptureHost()
    host.media.local_path = path
    host._do_live_thumb_capture()
    assert host._thumb_queue.requests == []


@pytest.mark.parametrize("index", [-1, 1])
def test_live_thumbnail_ignores_invalid_playlist_index(index):
    host = _CaptureHost()
    host._playlist_index = index
    host._do_live_thumb_capture()
    assert host._thumb_queue.requests == []


def test_audio_cover_feeds_queue_even_after_previous_attempt():
    host = _CaptureHost()
    host._is_audio = True
    host._request_thumbnail(0, host._playlist[0])
    cover = QPixmap(1, 1)
    host._audio_cover_pixmap = cover
    host._request_thumbnail(0, host._playlist[0])
    assert len(host._thumb_queue.requests) == 1
    assert host._thumb_queue.covers == [(0, cover)]
