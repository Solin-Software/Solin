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
