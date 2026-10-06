import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication, QFrame, QWidget

import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.fullscreen import FullscreenVideoOverlay


_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def own_widget(request):
    def own(widget: QWidget) -> QWidget:
        if isinstance(widget, FullscreenVideoOverlay):
            # Owner destruction schedules deletion of its parentless chrome.
            for frame in (widget._title_bar, widget._controls):
                request.addfinalizer(
                    lambda frame=frame: QCoreApplication.sendPostedEvents(
                        frame, QEvent.Type.DeferredDelete
                    )
                )

        def delete_widget() -> None:
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)

        request.addfinalizer(delete_widget)
        request.addfinalizer(widget.close)
        return widget

    return own


def test_active_projection_bar_surface_owns_click_cursor():
    bar = projection_bar.ProjectionBar.__new__(projection_bar.ProjectionBar)
    QFrame.__init__(bar)
    bar._container = None
    bar._volume = 0.5
    bar.setCursor(Qt.CursorShape.ArrowCursor)
    try:
        bar._build_bar_ui()

        assert bar.cursor().shape() is Qt.CursorShape.ArrowCursor
        assert bar.inactive_widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor)
        assert bar.inactive_widget.cursor().shape() is Qt.CursorShape.ArrowCursor
        assert bar.active_widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor)
        assert (
            bar.active_widget.cursor().shape()
            is Qt.CursorShape.PointingHandCursor
        )
        assert bar.proj_title.cursor().shape() is Qt.CursorShape.PointingHandCursor
    finally:
        bar.deleteLater()
        QCoreApplication.sendPostedEvents(bar, QEvent.Type.DeferredDelete)


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
    def __init__(self, *, active=False, native_output_active=False):
        self.active = active
        self.native_output_active = native_output_active
        self.native_video_surface = object()
        self.hidden = []
        self.reset_count = 0
        self.frames = []
        self.reconnect_states = []
        self.buffer_progress = []
        self.navigation = []
        self.play_enabled = []
        self.seek_enabled = []
        self.playback_options_active = []

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

    def set_native_output_active(self, active):
        self.native_output_active = active

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

    def set_playback_options_active(self, active):
        self.playback_options_active.append(active)


def _bar(*, mode="video", audio=False, overlay=None):
    bar = projection_bar.ProjectionBar.__new__(projection_bar.ProjectionBar)
    bar._mode = mode
    bar._is_audio = audio
    bar._expanded = False
    bar._playlist = []
    bar._playlist_index = 0
    bar._played_indices = {0}
    bar._loop = False
    bar._playback_order = projection_bar.ORDER_OFF
    bar._fullscreen_overlay = overlay
    bar._announce_state = "off"
    bar._auto_share_playback_waiting = False
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


def test_fullscreen_selects_native_presenter_before_showing_its_window(
    monkeypatch,
):
    events: list[tuple[str, bool | None]] = []

    class _EntryOverlay:
        def set_native_output_active(self, active):
            events.append(("native", active))

        def show_fullscreen(self):
            events.append(("show", None))

    overlay = _EntryOverlay()
    bar = _bar(mode="video", audio=False)
    bar.video_preview = SimpleNamespace(native_output_active=True)
    monkeypatch.setattr(bar, "_ensure_fullscreen_overlay", lambda: overlay)
    monkeypatch.setattr(bar, "_hydrate_fullscreen_overlay", lambda _overlay: None)

    bar.enter_app_fullscreen()

    assert events == [("native", True), ("show", None)]


def test_video_preview_creates_native_surface_only_when_route_is_enabled(own_widget):
    preview = own_widget(projection_bar._ThemedVideoPreview())
    preview.resize(1000, 800)

    assert preview.native_surface is None

    assert preview.set_native_output_active(True) is True

    assert preview.native_output_active is True
    assert preview.native_surface is not None
    # The surface is inset to the canvas aspect rather than filling the preview.
    # The sidecar's obs_display is only resized when a fresh window target reaches
    # it, so during a live window drag its size lags; a surface that can only ever
    # have the canvas aspect turns that stale display into a uniform scale instead
    # of a distortion. 1000x800 is narrower than 16:9, so the fit is width-bound and
    # centred vertically: 1000 / (16/9) = 562, offset (800 - 562) // 2 = 119.
    assert preview.native_surface.geometry().getRect() == (0, 119, 1000, 562)
    assert preview.native_surface.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert preview.native_surface.input_overlay is not None
    assert (
        preview.native_surface.input_overlay.cursor().shape()
        == Qt.CursorShape.ArrowCursor
    )

    # Each further case sizes a fresh preview before enabling its native route.

    # Narrower than the canvas: the fit is width-bound and centred vertically.
    narrow = own_widget(projection_bar._ThemedVideoPreview())
    narrow.resize(600, 800)
    narrow.set_native_output_active(True)
    assert narrow.native_surface.geometry().getRect() == (0, 231, 600, 338)

    # The aspect follows the document's canvas rather than an assumed 16:9.
    four_by_three = own_widget(projection_bar._ThemedVideoPreview())
    four_by_three.set_canvas_aspect(4 / 3)
    four_by_three.resize(1200, 600)
    four_by_three.set_native_output_active(True)
    assert four_by_three.native_surface.geometry().getRect() == (200, 0, 800, 600)


def test_fullscreen_replaces_expanded_native_target_instead_of_duplicating_it():
    class _Preview:
        def __init__(self):
            self.native_output_active = True
            self.states = []

        def set_native_output_active(self, active):
            changed = active != self.native_output_active
            self.native_output_active = active
            self.states.append(active)
            return changed

    overlay = _Overlay(active=True)
    preview = _Preview()
    bar = _bar(mode="video", audio=False, overlay=overlay)
    bar._expanded = True
    bar._video_preview_route_requested = True
    bar.video_preview = preview

    bar.set_native_video_output_active(True)

    assert overlay.native_output_active is True
    assert preview.states == [False]
    assert bar.native_video_output_surface is overlay.native_video_surface


def test_fullscreen_visibility_notifies_output_routing_without_forwarding_bool():
    notifications: list[None] = []

    class _Notification:
        def emit(self) -> None:
            notifications.append(None)

    host = SimpleNamespace(
        video_output_target_changed=_Notification()
    )

    projection_bar.ProjectionBar._on_fullscreen_visibility_changed(host, True)
    projection_bar.ProjectionBar._on_fullscreen_visibility_changed(host, False)

    assert notifications == [None, None]


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


def test_playback_options_indicator_follows_effective_auto_playback():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar._loop = False
    bar._playback_order = projection_bar.ORDER_NEXT
    bar._playlist = [{}, {}]
    bar._playlist_index = 0
    bar._played_indices = {0}
    bar.more_btn = _Button()
    bar.more_btn.setIcon = lambda _icon: None

    bar._refresh_playback_options_indicator()

    assert overlay.playback_options_active == [True]

    bar._playlist_index = 1
    bar._refresh_playback_options_indicator()

    assert overlay.playback_options_active == [True, False]


def test_playback_options_indicator_treats_loop_as_active_without_next_item():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar._loop = True
    bar._playback_order = projection_bar.ORDER_OFF
    bar._playlist = [{}]
    bar._playlist_index = 0
    bar._played_indices = {0}
    bar.more_btn = _Button()
    bar.more_btn.setIcon = lambda _icon: None

    bar._refresh_playback_options_indicator()

    assert overlay.playback_options_active == [True]


def test_playback_options_indicator_ignores_exhausted_random_order():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar._loop = False
    bar._playback_order = projection_bar.ORDER_RANDOM
    bar._playlist = [{}, {}]
    bar._playlist_index = 1
    bar._played_indices = {0, 1}
    bar.more_btn = _Button()
    bar.more_btn.setIcon = lambda _icon: None

    bar._refresh_playback_options_indicator()

    assert overlay.playback_options_active == [False]


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


def test_fullscreen_seek_respects_auto_share_playback_wait():
    bar = _bar(mode="video")
    bar.seek_requested = _ValueSignal()
    bar._auto_share_playback_waiting = True

    bar._on_fullscreen_seek_requested(1234)

    assert bar.seek_requested.values == []


def test_fullscreen_controls_mirror_announcement_enabled_state():
    overlay = _Overlay(active=True)
    bar = _bar(mode="video", overlay=overlay)
    bar.play_btn = _EnabledControl(enabled=True)
    bar.seek_slider = _EnabledControl(enabled=False)

    bar._sync_fullscreen_media_controls()

    assert overlay.play_enabled == [True]
    assert overlay.seek_enabled == [False]


def test_fullscreen_overlay_uses_parent_translator_and_original_control_order(own_widget):
    source = own_widget(QWidget())
    overlay = own_widget(
        FullscreenVideoOverlay(
            source_widget=source,
            translate=lambda text: f"pt:{text}",
            parent=source,
        )
    )

    assert overlay.play_btn.toolTip() == "pt:Pause/Resume"
    assert overlay.vol_btn.toolTip() == "pt:Volume"
    assert overlay.more_btn.toolTip() == "pt:Playback options"
    assert overlay.time_label.minimumWidth() < 40
    assert overlay._title_bar.isWindow()
    assert overlay._controls.isWindow()
    assert overlay._title_bar.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground
    )
    assert overlay._controls.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground
    )
    assert not overlay._title_bar.testAttribute(
        Qt.WidgetAttribute.WA_StyledBackground
    )
    assert not overlay._controls.testAttribute(
        Qt.WidgetAttribute.WA_StyledBackground
    )
    assert overlay._title_bar.windowFlags() & Qt.WindowType.NoDropShadowWindowHint
    assert overlay._controls.windowFlags() & Qt.WindowType.NoDropShadowWindowHint

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


def test_fullscreen_chrome_is_not_shown_from_the_owner_show_event(monkeypatch, own_widget):
    overlay = own_widget(FullscreenVideoOverlay(source_widget=None))
    calls: list[str] = []
    monkeypatch.setattr(
        overlay,
        "_show_chrome",
        lambda: calls.append("chrome"),
    )

    overlay.showEvent(QShowEvent())

    assert calls == []


def test_fullscreen_exit_hides_chrome_before_hiding_its_owner(
    monkeypatch, own_widget, request
):
    overlay = own_widget(FullscreenVideoOverlay(source_widget=None))
    # Restore window methods before the owner's registered cleanup runs.
    request.addfinalizer(monkeypatch.undo)
    order: list[str] = []
    monkeypatch.setattr(
        overlay._title_bar,
        "hide",
        lambda: order.append("title"),
    )
    monkeypatch.setattr(
        overlay._controls,
        "hide",
        lambda: order.append("controls"),
    )
    monkeypatch.setattr(overlay, "hide", lambda: order.append("owner"))

    overlay.hide_fullscreen()

    assert order == ["title", "controls", "owner"]
