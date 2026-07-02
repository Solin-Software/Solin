import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.fullscreen import FullscreenVideoOverlay


_APP = QApplication.instance() or QApplication([])


class _Button:
    def __init__(self):
        self.visible = None

    def setVisible(self, visible):
        self.visible = visible


class _Slider:
    def __init__(self):
        self.reconnect_states = []
        self.buffer_ratios = []

    def setReconnectActive(self, active):
        self.reconnect_states.append(active)

    def setBufferedRatio(self, ratio):
        self.buffer_ratios.append(ratio)


class _EnabledControl:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def isEnabled(self):
        return self.enabled


class _Protection:
    locked = False


class _ValueSignal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Overlay:
    def __init__(self, *, active=False):
        self.active = active
        self.hidden = []
        self.reset_count = 0
        self.frames = []
        self.reconnect_states = []
        self.buffer_progress = []
        self.navigation = []
        self.play_enabled = []
        self.seek_enabled = []

    def is_active(self):
        return self.active

    def hide_fullscreen(self, *, clear_frame=False):
        self.hidden.append(clear_frame)
        self.active = False

    def reset(self):
        self.reset_count += 1
        self.active = False

    def set_frame(self, frame):
        self.frames.append(frame)

    def set_reconnect_active(self, active):
        self.reconnect_states.append(active)

    def set_buffer_progress(self, downloaded, total):
        self.buffer_progress.append((downloaded, total))

    def set_navigation(self, *, show, can_previous, can_next):
        self.navigation.append((show, can_previous, can_next))

    def set_play_enabled(self, enabled):
        self.play_enabled.append(enabled)

    def set_seek_enabled(self, enabled):
        self.seek_enabled.append(enabled)


def _bar(*, mode="video", audio=False, overlay=None):
    bar = projection_bar.ProjectionBar.__new__(projection_bar.ProjectionBar)
    bar._mode = mode
    bar._is_audio = audio
    bar._expanded = False
    bar._playlist = []
    bar._playlist_index = 0
    bar._fullscreen_overlay = overlay
    bar.ov_fullscreen_btn = _Button()
    bar.seek_slider = _Slider()
    bar._playback_protection = _Protection()
    return bar


def test_app_fullscreen_is_available_only_for_visual_video():
    video = _bar(mode="video", audio=False)
    audio = _bar(mode="video", audio=True)
    image = _bar(mode="image", audio=False)

    assert video._is_app_fullscreen_available() is True
    assert audio._is_app_fullscreen_available() is False
    assert image._is_app_fullscreen_available() is False


def test_non_video_state_hides_app_fullscreen_and_button():
    overlay = _Overlay(active=True)
    bar = _bar(mode="image", overlay=overlay)

    bar._sync_app_fullscreen_availability()

    assert bar.ov_fullscreen_btn.visible is False
    assert overlay.hidden == [True]


def test_video_state_shows_app_fullscreen_button_without_closing_overlay():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", audio=False, overlay=overlay)

    bar._sync_app_fullscreen_availability()

    assert bar.ov_fullscreen_btn.visible is True
    assert overlay.hidden == []


def test_video_frames_reach_active_app_fullscreen_when_overlay_is_collapsed():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", audio=False, overlay=overlay)
    frame = object()

    bar._on_video_frame(frame)

    assert overlay.frames == [frame]


def test_audio_frames_do_not_reach_app_fullscreen():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", audio=True, overlay=overlay)

    bar._on_video_frame(object())

    assert overlay.frames == []


def test_recovery_feedback_is_mirrored_to_app_fullscreen():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)

    bar._on_playback_recovery_changed(True)
    bar._on_playback_recovery_changed(False)

    assert bar.seek_slider.reconnect_states == [True, False]
    assert overlay.reconnect_states == [True, False]


def test_buffer_progress_is_mirrored_to_app_fullscreen():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)

    bar._on_buffer_progress(25, 100)
    bar._on_buffer_progress(0, 0)

    assert bar._last_buffer_progress == (0, 0)
    assert bar.seek_slider.buffer_ratios == [0.25, 0.0]
    assert overlay.buffer_progress == [(25, 100), (0, 0)]


def test_playlist_navigation_state_is_mirrored_to_app_fullscreen():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar._playlist = [{}, {}, {}]
    bar._playlist_index = 1

    bar._sync_app_fullscreen_navigation()

    assert overlay.navigation == [(True, True, True)]


def test_fullscreen_seek_respects_song_announcement_lock():
    bar = _bar(mode="video")
    bar.seek_requested = _ValueSignal()

    bar._announce_state = "gate"
    bar._on_fullscreen_seek_requested(1234)

    bar._announce_state = "ready"
    bar._on_fullscreen_seek_requested(2345)

    bar._announce_state = "off"
    bar._on_fullscreen_seek_requested(3456)

    assert bar.seek_requested.values == [3456]


def test_fullscreen_controls_mirror_announcement_enabled_state():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar.play_btn = _EnabledControl(enabled=True)
    bar.seek_slider = _EnabledControl(enabled=False)

    bar._sync_fullscreen_announcement_controls()

    assert overlay.play_enabled == [True]
    assert overlay.seek_enabled == [False]


def test_fullscreen_overlay_uses_parent_translator_and_original_control_order():
    source = QWidget()
    overlay = FullscreenVideoOverlay(
        source_widget=source,
        translate=lambda text: f"pt:{text}",
        parent=source,
    )

    assert overlay.play_btn.toolTip() == "pt:Pause/Resume"
    assert overlay.vol_btn.toolTip() == "pt:Volume"
    assert overlay.more_btn.toolTip() == "pt:Playback options"
    assert overlay.time_label.minimumWidth() < 40

    layout = overlay._controls.layout()
    controls = [layout.itemAt(index).widget() for index in range(layout.count())]

    assert controls == [
        overlay.play_btn,
        overlay.seek_slider,
        overlay.time_label,
        overlay.vol_btn,
        overlay.vol_slider,
        overlay.more_btn,
        overlay.prev_btn,
        overlay.next_btn,
        overlay.stop_btn,
    ]
