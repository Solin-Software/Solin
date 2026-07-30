"""Drives the libobs :class:`ProjectionProgram` from Qt projection content.

The second monitor is composited entirely inside libobs (a fade transition on
channel 0 crossfading content scenes — see ``solin.core.media.obs_program``).
Media comes straight from the engine as an ``ffmpeg_source``; the remaining
content is Qt-rendered. This driver renders those Qt widgets (idle yeartext and
countdown timer) to a canvas-size ``QImage`` **once** — at full
projection resolution, independent of any window's size — and hands it to the
program as an image scene (crossfaded, or updated in place for the ticking
timer). Still pictures are passed through directly.

Process-wide singleton. Every projection surface fans the same content out; the
driver deduplicates by a content signature so redundant calls collapse into a
single crossfade, and each surface's libobs display renders the one shared
program.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from ..core.media.obs_program import projection_program
from ..core.media.obs_runtime import obs_runtime
from ..core.projection.image_framing import IDENTITY_IMAGE_TRANSFORM, ImageTransform
from ..core.projection.transform_animation import ProjectionTransformAnimation
from ..core.rendering.fonts import FontManager
from ..core.timer.models import MediaCountdownPresentation
from ..widgets.circular_timer import CircularTimerWidget
from .yearly_text import YearlyTextWidget

_TRANSFORM_TICK_MS = 16  # ~60 fps; libobs has no per-item tween, so the driver steps it

log = logging.getLogger(__name__)

_KEY_IDLE = "idle"
_KEY_IMAGE = "image"
_KEY_TIMER = "timer"
_KEY_BROWSER = "browser"
_KEY_CAMERA = "camera"
_KEY_NDI = "ndi"
_KEY_IDLE_VIDEO = "idle_video"
_KEY_IDLE_IMAGE = "idle_image"


def _camera_source_spec(device_path: str, device_name: str) -> tuple[str, dict]:
    """The libobs capture-source kind + settings for a camera, per platform.

    ``QCameraDevice.id()`` yields the platform-native device id — Linux:
    ``/dev/videoN``; Windows: the DirectShow device path; macOS: the AVFoundation
    ``uniqueID`` — and each OBS capture source expects it under a different key:

    * Linux  → ``v4l2_input``        ``device_id``
    * Windows→ ``dshow_input``       ``video_device_id`` (``"<name>:<path>"``)
    * macOS  → ``av_capture_input``  ``device``

    (Windows' dshow matches ``"<friendly name>:<device path>"``; the plain path is
    a best-effort fallback.) If the native source can't be created the caller
    keeps the Qt QCamera path, so an imperfect id degrades, it doesn't break.
    """
    if sys.platform == "win32":
        vid = f"{device_name}:{device_path}" if device_name else device_path
        return "dshow_input", {"video_device_id": vid, "last_video_device_id": vid}
    if sys.platform == "darwin":
        return "av_capture_input", {"device": device_path}
    return "v4l2_input", {"device_id": device_path}


def _image_signature(image: QImage) -> object:
    """A cheap content signature stable across separately-loaded copies."""
    if image is None or image.isNull():
        return None
    try:
        thumb = image.scaled(
            32,
            32,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_RGB32)
        return (image.width(), image.height(), bytes(thumb.constBits()))
    except Exception:  # noqa: BLE001 - signature is best-effort
        return (image.width(), image.height(), image.cacheKey())


class ProjectionProgramDriver:
    """Renders Qt projection content to canvas scenes for the program."""

    def __init__(self, font_manager: FontManager) -> None:
        self._program = projection_program()
        self._font_manager = font_manager
        # Offscreen render widgets, sized to the canvas on first use. Never shown;
        # ``render()`` drives their paintEvent into our image.
        self._yeartext = YearlyTextWidget(font_manager)
        self._timer = CircularTimerWidget()
        for widget in (self._yeartext, self._timer):
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        font_manager.font_ready.connect(self._on_font_ready)
        self._sized = False
        self._yeartext_data: tuple[str, str, str] = ("", "", "")
        self._countdown: tuple[int, int] | None = None
        self._timer_presentation: MediaCountdownPresentation | None = None
        self._sig: dict[str, object] = {}
        # Zoom/pan of the current static-image scene. libobs has no per-item
        # tween, so the same eased animator the Qt path uses is stepped by a
        # timer that pushes each frame's transform onto the live scene item.
        self._transform_anim = ProjectionTransformAnimation()
        self._transform_timer = QTimer()
        self._transform_timer.setInterval(_TRANSFORM_TICK_MS)
        self._transform_timer.timeout.connect(self._on_transform_tick)
        # Async libobs sources the live browser / NDI frames are pushed into
        # (created lazily, reused across re-shows — see show_browser_frame /
        # show_ndi_frame).
        self._browser_frames = None
        self._ndi_frames = None
        self._source_seq = 0  # unique names for created input sources (camera, idle)
        # Reused v4l2_input source for the camera (one per device; re-shown across
        # scenes so libobs opens/closes the device on activate/deactivate).
        self._camera_source = None
        self._camera_device: str | None = None
        # Paths of the custom idle background currently on the program (for dedup;
        # the sources themselves are owned by the program and released when the
        # idle scene is crossfaded away).
        self._idle_video_path: str | None = None
        self._idle_image_path: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def ensure(self) -> None:
        self._program.ensure()
        self._ensure_sized()

    def _ensure_sized(self) -> None:
        if self._sized:
            return
        width, height = self.canvas_size
        for widget in (self._yeartext, self._timer):
            widget.resize(width, height)
        self._sized = True

    @property
    def canvas_size(self) -> tuple[int, int]:
        canvas = obs_runtime().video
        return canvas.width, canvas.height

    @property
    def current_key(self) -> str | None:
        return self._program.current_key

    @property
    def is_blank(self) -> bool:
        """True when the shared program shows nothing real yet (see program)."""
        return self._program.is_blank

    # ── idle yeartext ─────────────────────────────────────────────────────

    def set_yeartext(self, quote: str, reference: str, api_code: str = "") -> None:
        """Update idle-text data; re-render live if idle is currently showing."""
        self._yeartext_data = (quote, reference, api_code)
        if self._program.current_key == _KEY_IDLE:
            self.show_yeartext()

    def show_yeartext(self) -> None:
        """Crossfade to the idle yeartext slide (rendered at canvas size)."""
        self.ensure()
        self._countdown = None
        self._yeartext.set_text(*self._yeartext_data)
        self._yeartext.clear_countdown()
        signature = (_KEY_IDLE, self._yeartext_data, None)
        self._crossfade(_KEY_IDLE, self._render(self._yeartext), signature)

    # ── still image ───────────────────────────────────────────────────────

    def show_static_image(
        self, image: QImage, *, transform: ImageTransform | None = None
    ) -> None:
        if image is None or image.isNull():
            return
        self.ensure()
        self._crossfade(
            _KEY_IMAGE, image, (_KEY_IMAGE, _image_signature(image)), zoomable=True
        )
        # A fresh scene item starts at identity; snap it to the (persisted) target
        # so no prior zoom bleeds in and replayed transforms land immediately.
        self._set_transform(transform or IDENTITY_IMAGE_TRANSFORM, animate=False)

    # ── zoom / pan ────────────────────────────────────────────────────────

    def set_image_transform(
        self, zoom: float, norm_x: float, norm_y: float, *, animate: bool = True
    ) -> None:
        """Zoom or pan the live static-image scene (animated by default)."""
        self._set_transform(ImageTransform(zoom, norm_x, norm_y), animate=animate)

    def reset_transform(self) -> None:
        self._set_transform(IDENTITY_IMAGE_TRANSFORM, animate=False)

    def _set_transform(self, transform: ImageTransform, *, animate: bool) -> None:
        active = self._transform_anim.set_target(transform, animate=animate)
        self._push_transform()
        if active:
            if not self._transform_timer.isActive():
                self._transform_timer.start()
        else:
            self._transform_timer.stop()

    def _push_transform(self) -> None:
        t = self._transform_anim.current
        self._program.set_image_transform(t.zoom, t.norm_x, t.norm_y)

    def _on_transform_tick(self) -> None:
        t = self._transform_anim.sample()
        self._program.set_image_transform(t.zoom, t.norm_x, t.norm_y)
        if not self._transform_anim.is_active:
            self._transform_timer.stop()

    # ── countdown timer ───────────────────────────────────────────────────

    def show_timer(
        self, remaining: int, total: int, presentation: MediaCountdownPresentation
    ) -> None:
        """Crossfade to the countdown (rendered from the selected widget)."""
        self.ensure()
        self._countdown = (remaining, total)
        self._timer_presentation = presentation
        image = self._render_timer(remaining, total, presentation)
        # Enter with a crossfade; per-second ticks update in place (no flicker).
        self._crossfade(_KEY_TIMER, image, (_KEY_TIMER, presentation), force=True)

    def update_timer(
        self, remaining: int, total: int, presentation: MediaCountdownPresentation
    ) -> None:
        """Tick the countdown in place (no crossfade)."""
        if self._program.current_key != _KEY_TIMER:
            self.show_timer(remaining, total, presentation)
            return
        self._countdown = (remaining, total)
        self._timer_presentation = presentation
        self._program.update_image(_KEY_TIMER, self._render_timer(remaining, total, presentation))

    def set_timer_blink(self, on: bool) -> None:
        """Blink the projected countdown's final-seconds cue (obs mode).

        Re-renders the current timer widget with the blink state toggled and
        swaps it into the live timer scene in place (no crossfade). No-op unless
        the countdown is the content currently showing on the program.
        """
        if self._program.current_key != _KEY_TIMER or self._countdown is None:
            return
        presentation = self._timer_presentation
        if presentation is None:
            return
        remaining, total = self._countdown
        if presentation is MediaCountdownPresentation.YEARLY_TEXT:
            self._yeartext.set_text(*self._yeartext_data)
            self._yeartext.set_countdown(remaining, total)
            self._yeartext.set_countdown_blink(on)
            image = self._render(self._yeartext)
        else:
            self._timer.update_data(remaining, total)
            self._timer.set_blink(on)
            image = self._render(self._timer)
        self._program.update_image(_KEY_TIMER, image)

    def _render_timer(
        self, remaining: int, total: int, presentation: MediaCountdownPresentation
    ) -> QImage:
        if presentation is MediaCountdownPresentation.YEARLY_TEXT:
            self._yeartext.set_text(*self._yeartext_data)
            self._yeartext.set_countdown(remaining, total)
            self._yeartext.set_countdown_blink(False)  # blink is driven by set_timer_blink
            return self._render(self._yeartext)
        self._timer.update_data(remaining, total)
        self._timer.set_blink(False)
        return self._render(self._timer)

    def show_browser_frame(self, image: QImage) -> bool:
        """Project the live browser by pushing its frame into a libobs async
        video source (``solin_frame_source``) that the program composites and
        crossfades — instead of capturing the browser's native window.

        The frame stream keeps producing frames while the browser page is hidden
        (WebKitGTK/CDP works off-screen), so this stays live across panel switches
        with no black flash and no window-mapping fragility. Called per frame:
        the first frame creates + crossfades to the source; every frame is pushed.
        Returns False if the frame source can't be created (plugin missing) so the
        caller keeps the Qt per-frame path.
        """
        if image is None or image.isNull():
            return False
        self.ensure()
        fs = self._browser_frames
        if fs is None:
            from ..core.media.obs_frame_source import create_frame_source

            fs = create_frame_source(obs_runtime())
            if fs is None:
                return False
            self._browser_frames = fs
        if self._program.current_key != _KEY_BROWSER:
            # (Re)enter the browser scene with a crossfade. The driver keeps the
            # frame source (owned=False) and reuses it across re-shows.
            self._program.show_source(_KEY_BROWSER, fs.source, owned=False)
        fs.push(image)
        return True

    def show_ndi_frame(self, image: QImage) -> bool:
        """Project an NDI / OBS-program stream by pushing its ctypes-decoded frame
        into a libobs async video source (``solin_frame_source``) the program
        composites and crossfades — instead of painting the QImage on a Qt widget
        with the libobs surface hidden.

        This is the SDK-free NDI bridge: Solin's existing libndi receiver keeps
        producing QImages, and they are pushed into libobs here (same mechanism as
        the browser). Called per frame; the first frame creates + crossfades to the
        source. Returns False if the frame source can't be created so the caller
        keeps the Qt per-frame path.
        """
        if image is None or image.isNull():
            return False
        self.ensure()
        fs = self._ndi_frames
        if fs is None:
            from ..core.media.obs_frame_source import create_frame_source

            fs = create_frame_source(obs_runtime())
            if fs is None:
                return False
            self._ndi_frames = fs
        if self._program.current_key != _KEY_NDI:
            self._program.show_source(_KEY_NDI, fs.source, owned=False)
        fs.push(image)
        return True

    def show_camera(self, device_path: str, device_name: str = "") -> bool:
        """Project a camera as a native libobs capture source — ``v4l2_input``
        (Linux), ``dshow_input`` (Windows) or ``av_capture_input`` (macOS), see
        :func:`_camera_source_spec`. libobs owns the device, decodes, composites
        and crossfades it. Returns False if the source can't be created so the
        caller keeps the Qt QCamera path.

        ONE source is created per device and **reused** across re-shows (the OBS
        model): crossfading away leaves the source but drops it from every scene,
        so libobs deactivates it and closes the device; re-showing re-adds it to a
        new scene, reactivating it and reopening the device. (Creating a fresh
        source each time raced the device close → the camera showed only once.)
        """
        if not device_path:
            return False
        self.ensure()
        if self._program.current_key == _KEY_CAMERA and self._camera_device == device_path:
            return True  # already projecting this camera
        source = self._camera_source
        if source is not None and self._camera_device != device_path:
            # A different camera was picked — drop the old source/device first.
            try:
                source.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Error releasing previous camera source", exc_info=True)
            source = None
            self._camera_source = None
        if source is None:
            ob = obs_runtime().ob
            self._source_seq += 1
            kind, settings = _camera_source_spec(device_path, device_name)
            try:
                source = ob.Source.create(kind, f"solin-camera-{self._source_seq}", settings)
            except Exception:  # noqa: BLE001 - source-creation / plugin boundary
                log.warning("Could not create %s for %s", kind, device_path, exc_info=True)
                return False
            if source is None:
                return False
            self._camera_source = source
            self._camera_device = device_path
        # owned=False: the driver keeps the reusable source; the crossfade only
        # disposes the wrapping scene (source deactivated → device closed).
        self._program.show_source(_KEY_CAMERA, source, owned=False)
        return True

    def show_idle_video(self, path: str) -> bool:
        """Project the custom idle background VIDEO as a native, looping, muted
        libobs ``ffmpeg_source`` — libobs decodes the file itself (no Qt/QImage),
        composites it and crossfades it on the program.

        One source per path; ``owned=True`` so it is released the moment we
        crossfade away (it is a ``_CAPTURE_KEY``), which stops libobs decoding
        while other content shows, and re-created (restarting the loop from 0)
        when the idle screen returns. Returns False if the source can't be built.
        """
        if not path:
            return False
        self.ensure()
        if self._program.current_key == _KEY_IDLE_VIDEO and self._idle_video_path == path:
            return True  # already looping this idle video
        ob = obs_runtime().ob
        self._source_seq += 1
        try:
            source = ob.Source.create(
                "ffmpeg_source",
                f"solin-idle-vid-{self._source_seq}",
                {"is_local_file": True, "local_file": path, "looping": True},
            )
        except Exception:  # noqa: BLE001 - source-creation / plugin boundary
            log.warning("Could not create idle ffmpeg_source for %s", path, exc_info=True)
            return False
        if source is None:
            return False
        # The idle background is always silent (mute + zero base volume: audio
        # monitoring may not honour the mute flag alone — see obs_program).
        try:
            source.muted = True
            source.volume = 0.0
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not silence idle video source", exc_info=True)
        self._idle_video_path = path
        self._program.show_source(_KEY_IDLE_VIDEO, source, owned=True)
        return True

    def show_idle_image(self, path: str) -> bool:
        """Project the custom idle background still IMAGE as a native libobs
        ``image_source`` (loaded straight from the file — no Qt/QImage),
        crossfaded on the program. Returns False if the source can't be built."""
        if not path:
            return False
        self.ensure()
        if self._program.current_key == _KEY_IDLE_IMAGE and self._idle_image_path == path:
            return True
        ob = obs_runtime().ob
        self._source_seq += 1
        try:
            source = ob.Source.create(
                "image_source", f"solin-idle-img-{self._source_seq}", {"file": path}
            )
        except Exception:  # noqa: BLE001 - source-creation / plugin boundary
            log.warning("Could not create idle image_source for %s", path, exc_info=True)
            return False
        if source is None:
            return False
        self._idle_image_path = path
        self._program.show_source(_KEY_IDLE_IMAGE, source, owned=True)
        return True

    def show_black(self) -> None:
        self.ensure()
        self._program.show_black()

    # ── internals ─────────────────────────────────────────────────────────

    def _crossfade(
        self, key: str, image: QImage, signature: object,
        *, force: bool = False, zoomable: bool = False,
    ) -> None:
        # Collapse the redundant per-surface fan-out into one crossfade: skip
        # only when this exact content is already the program's active scene.
        if not force and self._program.current_key == key and self._sig.get(key) == signature:
            return
        self._sig[key] = signature
        self._program.show_image(key, image, zoomable=zoomable)

    def _render(self, widget: QWidget) -> QImage:
        self._ensure_sized()
        width, height = self.canvas_size
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.black)
        widget.render(image)
        return image

    def _on_font_ready(self, _name: str) -> None:
        if self._program.current_key == _KEY_IDLE:
            self._sig.pop(_KEY_IDLE, None)  # force past the dedup
            self.show_yeartext()


_driver: ProjectionProgramDriver | None = None


def projection_program_driver(
    font_manager: FontManager | None = None,
) -> ProjectionProgramDriver:
    """Return the process-wide :class:`ProjectionProgramDriver` singleton."""
    global _driver
    if _driver is None:
        if font_manager is None:
            raise RuntimeError(
                "ProjectionProgramDriver not initialised; first call needs a FontManager"
            )
        _driver = ProjectionProgramDriver(font_manager)
    return _driver


__all__ = ["ProjectionProgramDriver", "projection_program_driver"]
