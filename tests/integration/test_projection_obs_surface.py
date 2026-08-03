"""Integration tests for the fully-libobs projection.

These compose the real projection page stack (BaseProjectionView) with a stub
FontManager, a mocked libobs Display and a fake projection-program driver, then
verify how the always-on native program surface and the Qt page stack are gated:

* media, idle yeartext, still images (including talk themes) and the countdown
  are libobs scenes the program crossfades — the native surface is shown and
  the driver is invoked;
* content that still streams through Qt (a live browser tab, a custom idle
  video) hides the surface so its Qt page shows through.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import solin.projection.program_driver as driver_mod
import solin.projection.window as window_mod
from solin.core.timer.models import MediaCountdownPresentation
from solin.projection.window import BaseProjectionView, ObsProjectionSurface


class _FakeFontManager(QObject):
    """Thread-free FontManager stand-in (the real one spawns download threads)."""

    font_ready = Signal(str)
    font_failed = Signal(str)

    def ensure(self, _font_name: str) -> None:
        pass

    def family(self, _font_name: str) -> str:
        return "Georgia"


class _FakeDriver:
    """Records ProjectionProgramDriver calls without libobs/rendering."""

    def __init__(self) -> None:
        self.current_key: str | None = None
        self.yeartext_calls = 0
        self.image_calls = 0
        self.black_calls = 0
        self.set_yeartext_calls: list[tuple[str, str, str]] = []
        self.timer_calls: list = []
        self.timer_updates: list = []
        self.blink_calls: list = []
        self.transform_calls: list = []
        self.reset_transform_calls = 0
        self.browser_frame_calls = 0
        self.ndi_frame_calls = 0
        self.camera_calls: list = []
        self.idle_video_calls: list = []
        self.idle_image_calls: list = []

    @property
    def is_blank(self) -> bool:
        return self.current_key in (None, "__black__")

    def show_idle_video(self, path) -> bool:
        self.idle_video_calls.append(path)
        self.current_key = "idle_video"
        return True

    def show_idle_image(self, path) -> bool:
        self.idle_image_calls.append(path)
        self.current_key = "idle_image"
        return True

    def show_yeartext(self) -> None:
        self.yeartext_calls += 1
        self.current_key = "idle"

    def show_black(self) -> None:
        self.black_calls += 1
        self.current_key = "__black__"

    def set_image_transform(self, zoom, norm_x, norm_y, *, animate=True) -> None:
        self.transform_calls.append((zoom, norm_x, norm_y, animate))

    def reset_transform(self) -> None:
        self.reset_transform_calls += 1

    def show_static_image(self, _image, *, transform=None) -> None:
        self.image_calls += 1
        self.current_key = "image"

    def show_browser_frame(self, _image) -> bool:
        self.browser_frame_calls += 1
        self.current_key = "browser"
        return True

    def show_ndi_frame(self, _image) -> bool:
        self.ndi_frame_calls += 1
        self.current_key = "ndi"
        return True

    def show_camera(self, device_path, device_name="") -> bool:
        self.camera_calls.append((device_path, device_name))
        self.current_key = "camera"
        return True

    def set_yeartext(self, quote: str, reference: str, api_code: str = "") -> None:
        self.set_yeartext_calls.append((quote, reference, api_code))

    def show_timer(self, remaining, total, presentation) -> None:
        self.timer_calls.append((remaining, total, presentation))
        self.current_key = "timer"

    def update_timer(self, remaining, total, presentation) -> None:
        self.timer_updates.append((remaining, total, presentation))

    def set_timer_blink(self, on) -> None:
        self.blink_calls.append(on)


class _Harness(BaseProjectionView):
    """Minimal concrete BaseProjectionView for exercising the page stack."""

    def __init__(self, font_manager: _FakeFontManager) -> None:
        super().__init__(None)
        self._font_manager = font_manager
        layout = QVBoxLayout(self)
        self._build_projection_stack(layout)

    def _obs_enter_idle(self) -> None:
        # Tests drive content explicitly; suppress the deferred auto-idle so a
        # queued singleShot cannot fire the real program/libobs in a later test.
        pass


def _harness(monkeypatch, tmp_path, *, obs_on: bool = True) -> _Harness:
    monkeypatch.setattr(window_mod, "obs_media_engine_active", lambda: obs_on)
    monkeypatch.setattr(ObsProjectionSurface, "ensure_display", lambda self: True)
    driver = _FakeDriver()
    monkeypatch.setattr(
        driver_mod, "projection_program_driver", lambda font_manager=None: driver
    )
    harness = _Harness(_FakeFontManager())
    harness._test_driver = driver
    return harness


def _image() -> QImage:
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(0)
    return img


def _standalone_surface() -> ObsProjectionSurface:
    """A surface whose parent is kept alive (Qt would delete a GC'd parent's child)."""
    parent = QWidget()
    surface = ObsProjectionSurface(parent)
    surface._test_parent = parent  # keep the parent referenced
    return surface


# ── program-surface gating ───────────────────────────────────────────────────


def test_surface_created_only_when_obs_engine_active(monkeypatch, tmp_path):
    assert _harness(monkeypatch, tmp_path, obs_on=True)._obs_surface is not None
    assert _harness(monkeypatch, tmp_path, obs_on=False)._obs_surface is None


def test_begin_video_qt_engine_shows_media_page_on_first_frame(monkeypatch, tmp_path):
    """Regression: in the DEFAULT Qt engine, begin_video() must not pre-set
    _is_showing_media — otherwise update_frame() never switches the page stack to
    the media page and the video stays invisible on the projection windows."""
    harness = _harness(monkeypatch, tmp_path, obs_on=False)
    assert harness._obs_surface is None

    harness.begin_video()
    assert harness._is_showing_media is False  # not shown until the first frame

    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0)
    harness.update_frame(image)

    assert harness._is_showing_media is True
    assert harness._stack.currentIndex() == harness._PAGE_MEDIA


def test_begin_video_shows_surface(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    assert harness._obs_surface.isHidden()

    harness.begin_video()

    assert not harness._obs_surface.isHidden()
    assert harness._is_showing_media is True


def test_static_image_shows_surface_and_crossfades(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    harness.show_image_from_qimage(_image())  # cache_pixmap default True → still

    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.image_calls == 1


def test_browser_frame_routes_to_program_and_shows_surface(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    ok = harness.show_browser_frame(_image())

    assert ok is True
    assert harness._test_driver.browser_frame_calls == 1
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.current_key == "browser"


def test_browser_frame_noop_without_obs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path, obs_on=False)
    assert harness.show_browser_frame(_image()) is False


def test_ndi_frame_routes_to_program_and_shows_surface(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    ok = harness.show_ndi_frame(_image())

    assert ok is True
    assert harness._test_driver.ndi_frame_calls == 1
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.current_key == "ndi"


def test_ndi_frame_noop_without_obs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path, obs_on=False)
    assert harness.show_ndi_frame(_image()) is False


def test_camera_routes_to_program_and_shows_surface(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    ok = harness.show_camera("/dev/video0")

    assert ok is True
    assert harness._test_driver.camera_calls == [("/dev/video0", "")]
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.current_key == "camera"


def test_camera_noop_without_obs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path, obs_on=False)
    assert harness.show_camera("/dev/video0") is False


def test_streaming_frame_hides_surface(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.begin_video()

    harness.show_image_from_qimage(_image(), cache_pixmap=False)  # live-tab stream

    assert harness._obs_surface.isHidden()
    assert harness._test_driver.image_calls == 0


def test_show_timer_shows_surface_and_crossfades(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    harness.show_timer(10, 20, MediaCountdownPresentation.CIRCULAR)

    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.timer_calls == [(10, 20, MediaCountdownPresentation.CIRCULAR)]


def test_update_timer_ticks_in_place(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.show_timer(10, 20, MediaCountdownPresentation.CIRCULAR)

    harness.update_timer(9, 20)

    assert harness._test_driver.timer_updates == [(9, 20, MediaCountdownPresentation.CIRCULAR)]


def test_timer_blink_routes_to_program_in_obs_mode(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.show_timer(3, 20, MediaCountdownPresentation.CIRCULAR)

    harness.set_timer_blink(True)
    harness.set_timer_blink(False)

    # The final-seconds blink now re-renders the libobs timer scene (it used to be
    # a no-op on hidden Qt widgets in obs mode).
    assert harness._test_driver.blink_calls == [True, False]


def test_clear_shows_program_idle(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.begin_video()

    harness.clear()
    QApplication.processEvents()  # clear() defers its idle crossfade one tick

    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.yeartext_calls == 1
    assert harness._is_showing_media is False


def test_video_after_image_goes_straight_to_media(monkeypatch, tmp_path):
    """image → play video must not flash the yeartext in between.

    project_video_core does stop → clear() → begin_video() in one tick; clear()
    defers the idle crossfade so begin_video() cancels it and the projection goes
    image → media directly.
    """
    harness = _harness(monkeypatch, tmp_path)
    harness.show_image_from_qimage(_image())
    assert harness._test_driver.current_key == "image"

    harness.clear()
    harness.begin_video()
    QApplication.processEvents()  # would fire the deferred idle if not cancelled

    assert harness._test_driver.yeartext_calls == 0  # no yeartext flash
    assert not harness._obs_surface.isHidden()


def test_image_after_image_goes_straight_to_image(monkeypatch, tmp_path):
    """image → another image must not flash the yeartext in between."""
    harness = _harness(monkeypatch, tmp_path)
    harness.show_image_from_qimage(_image())

    harness.clear()
    harness.show_image_from_qimage(_image())
    QApplication.processEvents()  # would fire the deferred idle if not cancelled

    assert harness._test_driver.yeartext_calls == 0  # no yeartext flash
    assert harness._test_driver.image_calls == 2


def test_obs_idle_video_composites_in_libobs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    # Idle program (yeartext) — safe for the custom idle to replace.
    harness._test_driver.current_key = "idle"

    harness.show_obs_idle_media("/media/idle.mp4", "video")

    # Composited by libobs (native ffmpeg_source), surface stays up — NOT the
    # old Qt IdleMediaWidget path that hid the surface.
    assert harness._test_driver.idle_video_calls == ["/media/idle.mp4"]
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.current_key == "idle_video"


def test_obs_idle_image_composites_in_libobs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness._test_driver.current_key = "idle"

    harness.show_obs_idle_media("/media/idle.png", "image")

    assert harness._test_driver.idle_image_calls == ["/media/idle.png"]
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.current_key == "idle_image"


def test_obs_idle_media_does_not_override_live_content(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.begin_video()  # a clip is projecting
    harness._test_driver.current_key = "media"

    harness.show_obs_idle_media("/media/idle.mp4", "video")

    # Recorded for later, but not shown now — the clip keeps the program.
    assert harness._test_driver.idle_video_calls == []
    assert harness._test_driver.current_key == "media"


def test_clear_returns_to_custom_idle_video_on_program(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.show_obs_idle_media("/media/idle.mp4", "video")  # configured while idle
    harness.begin_video()  # then a clip plays over it
    harness._test_driver.idle_video_calls.clear()

    harness.clear()

    # Stopping the clip crossfades the program back to the custom idle video
    # (surface up) — the crossfade disposes the just-detached media source.
    assert harness._test_driver.idle_video_calls == ["/media/idle.mp4"]
    assert not harness._obs_surface.isHidden()
    assert harness._test_driver.black_calls == 0


def test_clear_idle_crossfades_program_to_yeartext(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.show_obs_idle_media("/media/idle.mp4", "video")
    assert harness._test_driver.current_key == "idle_video"

    harness.clear_idle()  # user removed the custom idle

    assert harness._test_driver.yeartext_calls == 1
    assert harness._test_driver.current_key == "idle"
    assert not harness._obs_surface.isHidden()


def test_clear_idle_does_not_reset_shared_program_when_content_shows(monkeypatch, tmp_path):
    # clear_idle also fires on window creation/reconcile (controller's
    # apply_full_state_to_window with no custom idle). It must NOT crossfade the
    # SHARED channel-0 program to the yeartext when live content is showing (here
    # or on another surface) — that would blank the media on the real monitor.
    harness = _harness(monkeypatch, tmp_path)
    harness._test_driver.current_key = "media"  # another surface drives live media

    harness.clear_idle()

    assert harness._test_driver.yeartext_calls == 0  # shared program left intact


def test_set_yearly_text_updates_driver(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    harness.set_yearly_text("Quote", "Ref 1:1", "code")

    assert harness._test_driver.set_yeartext_calls == [("Quote", "Ref 1:1", "code")]


def test_zoom_transform_routes_to_program_for_image(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness._test_driver.current_key = "image"

    harness.set_image_transform(2.0, 0.1, -0.1, animate=False)

    assert harness._test_driver.transform_calls == [(2.0, 0.1, -0.1, False)]


def test_zoom_transform_ignored_for_media(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness._test_driver.current_key = "media"  # video carries no transform

    harness.set_image_transform(2.0, 0.1, -0.1)

    assert harness._test_driver.transform_calls == []


def test_first_surface_seeds_idle_yeartext(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)  # driver blank (current_key None)

    BaseProjectionView._obs_enter_idle(harness)  # the real (unpatched) attach

    assert harness._test_driver.yeartext_calls == 1
    assert not harness._obs_surface.isHidden()


def test_late_surface_does_not_reset_shared_program(monkeypatch, tmp_path):
    # The window preview opened mid-playback attaches to the shared channel-0
    # program — it must render the existing content, not reset it to the idle
    # yeartext (that would blank the media on the real monitor).
    harness = _harness(monkeypatch, tmp_path)
    harness._test_driver.current_key = "media"  # another surface already drives it

    BaseProjectionView._obs_enter_idle(harness)

    assert harness._test_driver.yeartext_calls == 0  # shared program left intact
    assert not harness._obs_surface.isHidden()  # this surface still comes up


def test_reentering_video_keeps_surface_shown(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.begin_video()
    harness.show_image_from_qimage(_image())  # still picture → surface stays on
    assert not harness._obs_surface.isHidden()

    harness.begin_video()

    assert not harness._obs_surface.isHidden()


def test_no_surface_path_does_not_crash_without_obs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path, obs_on=False)
    harness.begin_video()
    harness.show_image_from_qimage(_image())
    harness.clear()
    assert harness._obs_surface is None


def test_update_frame_ignored_under_obs(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    harness.begin_video()

    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0)
    harness.update_frame(image)

    # The engine's per-source frame_ready is only for the operator preview; the
    # projection paints media through the libobs program instead.
    assert harness.display_label._pending_video_image is None


# ── ObsProjectionSurface contract ────────────────────────────────────────────


def test_surface_disables_qt_painting():
    surface = _standalone_surface()
    assert surface.paintEngine() is None


def test_surface_release_and_enable_are_safe_without_display():
    surface = _standalone_surface()
    surface.set_enabled(True)
    surface.release()
    assert surface._display is None


class _LiveDisplay:
    def __init__(self) -> None:
        self.enabled = None
        self.released = 0
        self.sizes: list[tuple[int, int]] = []

    def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))

    def release(self) -> None:
        self.released += 1


def test_set_enabled_toggles_live_display():
    surface = _standalone_surface()
    surface._display = _LiveDisplay()

    surface.set_enabled(True)
    assert surface._display.enabled is True
    surface.set_enabled(False)
    assert surface._display.enabled is False


def test_release_destroys_live_display():
    surface = _standalone_surface()
    display = _LiveDisplay()
    surface._display = display

    surface.release()

    assert display.released == 1
    assert surface._display is None


def test_sync_geometry_resizes_live_display():
    from PySide6.QtCore import QRect

    surface = _standalone_surface()
    surface._display = _LiveDisplay()

    surface.sync_geometry(QRect(0, 0, 800, 600))

    assert surface._display.sizes == [(800, 600)]


def test_ensure_display_wires_libobs_display(monkeypatch):
    """ensure_display should create a Display for the window and add a draw callback."""

    created: dict[str, object] = {}

    class _FakeDisplay:
        def __init__(self) -> None:
            self.callbacks: list = []

        def add_draw_callback(self, fn) -> None:
            self.callbacks.append(fn)

    class _FakeDisplayFactory:
        @staticmethod
        def from_window(handle, width, height, background_color=0xFF1A1A1A):
            created["handle"] = handle
            created["size"] = (width, height)
            created["background_color"] = background_color
            display = _FakeDisplay()
            created["display"] = display
            return display

    render_calls: list[tuple] = []

    class _FakeOb:
        Display = _FakeDisplayFactory

        @staticmethod
        def render_main_texture_letterboxed(*args) -> None:
            render_calls.append(args)

    class _FakeRuntime:
        video = type("V", (), {"width": 1920, "height": 1080})()
        ob = _FakeOb()

        def ensure_started(self) -> None:
            pass

    monkeypatch.setattr(
        "solin.core.media.obs_runtime.obs_runtime", lambda: _FakeRuntime()
    )

    surface = _standalone_surface()
    surface.resize(640, 360)
    ok = surface.ensure_display()

    assert ok is True
    assert created["size"] == (640, 360)
    assert created["background_color"] == 0xFF000000  # black letterbox bars
    assert created["handle"] == int(surface.winId())
    assert len(created["display"].callbacks) == 1

    created["display"].callbacks[0](640, 360)
    assert render_calls == [(1920, 1080, 640, 360)]

    assert surface.ensure_display() is True
