import sys
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QSignalSpy

from solin.controllers.playback_protection_controller import PlaybackProtectionController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.settings_store import SettingsStore
from solin.core.media.playback_state import SolinPlaybackState
from solin.core.media.profile_store import ProfileMediaStore
from solin.core.media.settings import MediaSettingsStore, ProjectionPlaybackSettingsStore
from solin.core.projection.image_framing import IDENTITY_IMAGE_TRANSFORM, ImageTransform
from solin.core.rendering.fonts import FontManager
from solin.ui.incremental_load import IncrementalLoadState
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


class _ThumbnailQueue(QObject):
    info_ready = Signal(int, QPixmap, str)

    def clear(self):
        pass


class _PlaybackSettings:
    def playback_order(self):
        return "off"


class _Media(QObject):
    state_changed = Signal(object)
    duration_changed = Signal(int)
    position_changed = Signal(int)
    media_ended = Signal()
    playback_source_changed = Signal(bool)
    buffer_progress = Signal(int, int)
    playback_recovery_changed = Signal(bool)

    is_playing = False

    def set_volume(self, _volume):
        pass

    def set_playing(self, playing):
        self.is_playing = playing
        self.state_changed.emit(
            SolinPlaybackState.PLAYING if playing else SolinPlaybackState.PAUSED
        )


@pytest.fixture
def projection_controls(tmp_path, monkeypatch):
    settings = SettingsStore.for_namespace(f"projection-lifecycle-{uuid.uuid4().hex}", "test")
    media = _Media()
    protection = PlaybackProtectionController(MediaSettingsStore(settings), media)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    bar = None
    try:
        bar = projection_bar.ProjectionBar(
            media,
            playback_settings=ProjectionPlaybackSettingsStore(settings),
            playback_protection=protection,
            profile_paths=Mock(spec=ProfilePaths),
            profile_media_store=Mock(spec=ProfileMediaStore),
            media_cache_dir=tmp_path,
            media_info_queue_factory=_ThumbnailQueue,
            font_manager=Mock(spec=FontManager),
            yearly_text_provider=lambda: ("", "", ""),
        )
        yield SimpleNamespace(bar=bar, media=media, protection=protection, errors=errors)
    finally:
        if bar is not None:
            bar.overlay_preparation_handle.cancel()
            # The final construction unit attaches these staged widgets to the overlay.
            for name in ("overlay_stack", "_overlay_body"):
                staged = getattr(bar, name, None)
                if staged is not None and staged.parent() is None:
                    staged.deleteLater()
            bar.close()
            bar.deleteLater()
        protection.deleteLater()
        media.deleteLater()
        settings.clear()


@pytest.mark.parametrize("initial_enabled", [False, True])
@pytest.mark.parametrize("item_count", [0, 3])
@pytest.mark.parametrize("completed_units", range(4))
def test_protection_changes_preserve_deferred_overlay(
    projection_controls, initial_enabled, item_count, completed_units,
):
    controls = projection_controls
    bar = controls.bar
    handle = bar.overlay_preparation_handle
    if completed_units:
        handle.start()
        for _ in range(completed_units):
            handle._run_next_unit()
    overlay = bar.overlay
    state = handle.state
    bar._playlist = [{"title": str(index)} for index in range(item_count)]
    bar._playlist_index = max(0, item_count - 2)

    controls.protection.set_enabled(initial_enabled)
    controls.protection.set_enabled(not initial_enabled)
    controls.media.set_playing(True)
    assert bar.seek_slider.isEnabled() is initial_enabled
    if item_count > 1:
        assert not bar.prev_btn.isHidden()
        assert not bar.next_btn.isHidden()
        assert bar.prev_btn.isEnabled() is initial_enabled
        assert bar.next_btn.isEnabled() is initial_enabled
    controls.protection.set_enabled(initial_enabled)
    assert bar.seek_slider.isEnabled() is not initial_enabled
    if item_count > 1:
        assert bar.prev_btn.isEnabled() is not initial_enabled
        assert bar.next_btn.isEnabled() is not initial_enabled
    controls.media.set_playing(False)

    assert controls.errors == []
    assert bar.seek_slider.isEnabled()
    assert bar.overlay is overlay
    assert not bar._overlay_ready
    assert handle.state is state


@pytest.mark.parametrize("item_count", [0, 1, 3])
@pytest.mark.parametrize("from_saved_playlist", [False, True])
@pytest.mark.parametrize("locked", [False, True])
def test_overlay_completion_and_protection_changes_sync_current_navigation(
    projection_controls, item_count, from_saved_playlist, locked,
):
    controls = projection_controls
    bar = controls.bar
    controls.protection.set_enabled(True)
    controls.media.set_playing(locked)
    bar._playlist = [{"title": str(index)} for index in range(item_count)]
    bar._playlist_index = max(0, item_count - 2)
    bar._is_from_saved_playlist = from_saved_playlist

    bar._ensure_overlay_ready()

    assert bar.overlay_preparation_handle.state is IncrementalLoadState.READY
    assert bar._overlay_ready
    show = item_count > 1
    assert bar.prev_btn.isHidden() is not show
    assert bar.next_btn.isHidden() is not show
    assert bar.ov_panel_btn.isHidden() is not show
    assert bar.ov_send_temp_btn.isHidden() is not (show and not from_saved_playlist)
    if show:
        assert bar.prev_btn.isEnabled() is not locked
        assert bar.next_btn.isEnabled() is not locked
        controls.media.set_playing(not locked)
        assert bar.prev_btn.isEnabled() is locked
        assert bar.next_btn.isEnabled() is locked
    assert controls.errors == []


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


@pytest.mark.parametrize("option", ["aspect", "bounds"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("draft", "constrained"),
    [
        (IDENTITY_IMAGE_TRANSFORM, IDENTITY_IMAGE_TRANSFORM),
        (ImageTransform(2.0, 1.5, -1.5), ImageTransform(2.0, 0.5, -0.5)),
        (ImageTransform(0.5, 0.2, -0.2), ImageTransform(0.5, 0.0, 0.0)),
    ],
    ids=["identity", "out-of-bounds-pan", "zoomed-out"],
)
def test_image_framing_options_only_update_preview_until_apply(
    projection_controls, option, enabled, draft, constrained,
):
    bar = projection_controls.bar
    bar._ensure_overlay_ready()
    preview = bar.preview_content
    preview.resize(1600, 900)
    preview.set_image_mode(True)
    pixmap = QPixmap(1600, 900)
    pixmap.fill(QColor("white"))
    preview.set_image_pixmap_fresh(pixmap)
    bar._on_image_match_projection_aspect_changed(not enabled if option == "aspect" else True)
    bar._on_image_constrain_to_frame_changed(not enabled if option == "bounds" else True)
    previous = preview.set_current_transform(draft)
    applied = QSignalSpy(bar.image_apply_transform)
    applied_instantly = QSignalSpy(bar.image_apply_transform_instant)
    reset = QSignalSpy(bar.image_reset_transform)

    button = preview._aspect_btn if option == "aspect" else preview._bounds_btn
    button.click()

    assert button.isChecked() is enabled
    settings = bar._playback_settings
    saved = (
        settings.image_match_projection_aspect()
        if option == "aspect"
        else settings.image_constrain_to_frame()
    )
    assert saved is enabled
    current = preview.current_transform()
    if option == "aspect":
        assert current == IDENTITY_IMAGE_TRANSFORM
    elif enabled:
        assert current == constrained
    else:
        assert current == previous
    assert applied.count() == 0
    assert applied_instantly.count() == 0
    assert reset.count() == 0

    preview._apply_btn.click()

    assert applied.count() == 1
    assert applied.at(0) == [current.zoom, current.norm_x, current.norm_y]
    assert applied_instantly.count() == 0
    assert reset.count() == 0
    assert projection_controls.errors == []


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
