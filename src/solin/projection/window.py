import ctypes
import sys

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    Signal,
    QSize,
    Slot,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.projection.image_framing import IDENTITY_IMAGE_TRANSFORM, ImageTransform
from ..core.projection.transform_animation import ProjectionTransformAnimation
from ..core.rendering.fonts import FontManager
from ..core.timer.models import MediaCountdownPresentation
from ..widgets.circular_timer import CircularTimerWidget
from ..widgets.projection.native_surface import NativeVideoSurface
from .yearly_text import YearlyTextWidget


def exclude_from_aero_peek(hwnd: int) -> None:
    """
    Tells the Windows Desktop Window Manager to exclude this window from
    Aero Peek / taskbar thumbnail hover previews.

    When the user hovers over another app's thumbnail in the taskbar,
    Windows temporarily hides all other windows to show a preview — this
    makes the projection window disappear from the secondary monitor.
    Setting DWMWA_EXCLUDED_FROM_PEEK prevents that behaviour.
    """
    if sys.platform != "win32":
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        # DWMWA_EXCLUDED_FROM_PEEK = 12
        DWMWA_EXCLUDED_FROM_PEEK = 12
        value = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_EXCLUDED_FROM_PEEK),
            ctypes.byref(value),
            ctypes.c_uint(ctypes.sizeof(value)),
        )
    except Exception:  # noqa: BLE001 - Win32 window-manager API boundary
        log_ignored_exception(__name__, "Could not exclude projection window from Aero Peek")

# ─────────────────────────────────────────────────────────────────────────────
# IdleMediaWidget — replaces the yeartext when a custom idle media is set
# Supports images (static) and videos (looped, muted).
# Created once per projection surface; hidden by default.
# ─────────────────────────────────────────────────────────────────────────────

class IdleMediaWidget(QWidget):
    """
    Passive, full-screen renderer for the custom idle background.

    This widget does **no decoding of its own**.  Both static images and video
    frames are decoded *once* by the shared :class:`solin.projection.idle_source.
    IdleMediaSource` and handed to every surface via :meth:`set_image`.  This is
    the design that keeps all monitors frame-locked and collapses N independent
    decoders down to one (see ``idle_source.py`` for the full rationale).

    Everything is painted manually in :meth:`paintEvent` via ``QPainter`` so
    ``QGraphicsOpacityEffect`` composites correctly (a native ``QVideoWidget``
    overlay would bypass the painter pipeline and ignore the fade effect).

    A single incoming :class:`QImage` is shared, read-only, across all surfaces
    on the GUI thread; each surface scales it to its own geometry on the GPU via
    ``SmoothPixmapTransform`` with a cached destination rect — no CPU pre-scale.

    Visibility signalling
    ---------------------
    Emits :attr:`visibility_changed` whenever this page is actually shown/hidden
    (the QStackedWidget swaps it in/out as the projection switches between idle
    and live content).  The controller uses this to pause the shared video
    decoder while *no* surface is showing the idle screen — so a custom idle
    video costs zero CPU while a clip/image/timer is being projected.
    """

    #: True when this page becomes visible, False when hidden.  Lets the owner
    #: pause idle-video decoding while nothing is showing it.
    visibility_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: black;")

        # Latest frame to paint (static image or current video frame).  Shared
        # with the other surfaces — never mutate it, only replace the reference.
        self._image: QImage | None = None
        self._paint_pending: bool = False
        self._reported_visible: bool = False

        # ── Geometry cache (same pattern as VideoDisplayWidget) ───────────
        self._cached_widget_size: QSize = QSize()
        self._cached_src_size: QSize = QSize()
        self._cached_dst_rect: QRectF = QRectF()

    # ── Visibility signalling ──────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._report_visibility(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._report_visibility(False)

    def _report_visibility(self, visible: bool) -> None:
        # Guard against duplicate/spontaneous events so the signal only fires on
        # a real transition.
        if visible != self._reported_visible:
            self._reported_visible = visible
            self.visibility_changed.emit(visible)

    # ── Public API ────────────────────────────────────────────────────────

    def set_image(self, image: QImage) -> None:
        """Paint a frame decoded by the shared idle source (image or video)."""
        if image is None or image.isNull():
            return
        self._image = image
        # Invalidate the dst cache when the source dimensions change
        # (first frame, switching media, or a mid-stream format change).
        if QSize(image.width(), image.height()) != self._cached_src_size:
            self._cached_src_size = QSize()
        if not self._paint_pending:
            self._paint_pending = True
            self.update()

    def clear(self) -> None:
        """Drop the current frame and go black."""
        self._image = None
        self._paint_pending = True
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        self._paint_pending = False
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._image is not None and not self._image.isNull():
            dst = self._ensure_dst_rect(self._image.width(), self._image.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(dst, self._image)

        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Invalidate geometry caches so the next paint recalculates the fit.
        self._cached_widget_size = QSize()
        self._cached_src_size = QSize()
        if self._image is not None:
            self.update()

    # ── Internals ─────────────────────────────────────────────────────────

    def _ensure_dst_rect(self, src_w: int, src_h: int) -> QRectF:
        """Return the centred keep-aspect-ratio destination rect for the frame."""
        widget_size = self.size()
        src_size = QSize(src_w, src_h)
        if widget_size == self._cached_widget_size and src_size == self._cached_src_size:
            return self._cached_dst_rect

        self._cached_widget_size = QSize(widget_size)
        self._cached_src_size = QSize(src_size)

        ww, wh = widget_size.width(), widget_size.height()
        if src_w <= 0 or src_h <= 0 or ww <= 0 or wh <= 0:
            self._cached_dst_rect = QRectF(0, 0, ww, wh)
            return self._cached_dst_rect

        scale = min(ww / src_w, wh / src_h)
        dw = src_w * scale
        dh = src_h * scale
        self._cached_dst_rect = QRectF(
            (ww - dw) / 2.0, (wh - dh) / 2.0, dw, dh
        )
        return self._cached_dst_rect


class VideoDisplayWidget(QWidget):
    """
    High-performance video/image display widget.

    Video path (per frame):
      • Receives QVideoFrame via set_video_frame() — no conversion yet.
      • Calls self.update() *only once* per pending paint (throttle flag).
      • paintEvent converts the frame to QImage and draws it directly onto
        the widget via QPainter.drawImage(), with SmoothPixmapTransform
        render hint so the GPU compositor handles the scaling instead of
        doing pixmap.scaled() on the CPU.
      • The aspect-ratio destination rect is cached and recalculated only
        when the widget is resized.

    Image path (static content):
      • Receives QImage via set_image() — stored and painted the same way.
      • Keeps a QPixmap copy for faster subsequent paints (no CPU re-scale).

    Zoom/pan transform (images only):
      • set_image_transform(zoom, norm_x, norm_y) applies an offset on top
        of the centred fit-to-widget base rect.
      • Uses a time-based premium easing curve so projector motion stays smooth.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: black;")

        # ── video state ───────────────────────────────────────────────────
        self._video_frame: QVideoFrame | None = None
        self._video_image: QImage | None = None
        self._paint_pending: bool = False

        # ── image (static) state ─────────────────────────────────────────
        self._static_image: QImage | None = None
        self._mode: str = "black"  # "black" | "video" | "image"

        # ── cached geometry ───────────────────────────────────────────────
        self._cached_widget_size: QSize = QSize()
        self._cached_src_size: QSize = QSize()
        self._cached_dst_rect: QRectF = QRectF()

        # ── Zoom/pan transform (images only) ─────────────────────────────
        self._image_transform = ProjectionTransformAnimation()
        self._transform_timer = QTimer(self)
        self._transform_timer.setInterval(16)
        self._transform_timer.timeout.connect(self._on_transform_tick)

    # ── public API ────────────────────────────────────────────────────────

    def set_video_frame(self, frame: QVideoFrame) -> None:
        """Accept a new video frame (called up to 60× per second)."""
        if not frame.isValid():
            return
        self._video_frame = frame
        self._mode = "video"
        if not self._paint_pending:
            self._paint_pending = True
            self.update()

    def set_image(
        self,
        image: QImage,
        *,
        initial_transform: ImageTransform | None = None,
    ) -> None:
        """Display a static QImage (replaces any active video)."""
        self._video_frame = None
        self._video_image = None
        self._static_image = image
        self._mode = "image"
        self._cached_src_size = QSize()
        transform = initial_transform or IDENTITY_IMAGE_TRANSFORM
        self.set_image_transform(
            transform.zoom,
            transform.norm_x,
            transform.norm_y,
            animate=False,
        )
        self._paint_pending = True
        self.update()

    def clear(self) -> None:
        """Go black — clear any displayed content."""
        self._video_frame = None
        self._video_image = None
        self._static_image = None
        self._mode = "black"
        # Reset transform without animation
        self._image_transform.reset()
        self._transform_timer.stop()
        self._paint_pending = True
        self.update()

    def set_image_transform(self, zoom: float, norm_x: float, norm_y: float,
                            *, animate: bool = True) -> None:
        """
        Set a new zoom/pan target. By default the change uses the shared,
        time-based projection animation with interruption-safe retargeting.

        Pass ``animate=False`` to snap to the target instantly — used when
        replaying the current transform onto a freshly created surface (a
        hot-plugged monitor / respawned preview) so it appears already framed
        instead of animating in from identity.

        norm_x / norm_y are offsets expressed as a fraction of the widget size.
        """
        active = self._image_transform.set_target(
            ImageTransform(zoom, norm_x, norm_y),
            animate=animate,
        )
        if active:
            self._transform_timer.start()
        else:
            self._transform_timer.stop()
            self._paint_pending = True
            self.update()

    def reset_transform_instant(self) -> None:
        """Snap transform to identity immediately — no lerp animation.
        Used when switching to a new image so the old zoom/pan never bleeds through.
        """
        self._image_transform.reset()
        self._transform_timer.stop()

    # ── Transform animation tick ──────────────────────────────────────────

    def _on_transform_tick(self):
        self._image_transform.sample()
        if not self._image_transform.is_active:
            self._transform_timer.stop()

        if not self._paint_pending:
            self._paint_pending = True
            self.update()

    # ── internals ─────────────────────────────────────────────────────────

    def _ensure_dst_rect(self, src_w: int, src_h: int) -> QRectF:
        """Return the centred keep-aspect-ratio destination rect.
        Recalculated only when the widget size or source size changes."""
        widget_size = self.size()
        src_size = QSize(src_w, src_h)
        if widget_size == self._cached_widget_size and src_size == self._cached_src_size:
            return self._cached_dst_rect

        self._cached_widget_size = QSize(widget_size)
        self._cached_src_size = QSize(src_size)

        ww, wh = widget_size.width(), widget_size.height()
        if src_w <= 0 or src_h <= 0 or ww <= 0 or wh <= 0:
            self._cached_dst_rect = QRectF(0, 0, ww, wh)
            return self._cached_dst_rect

        scale = min(ww / src_w, wh / src_h)
        dw = src_w * scale
        dh = src_h * scale
        dx = (ww - dw) / 2
        dy = (wh - dh) / 2
        self._cached_dst_rect = QRectF(dx, dy, dw, dh)
        return self._cached_dst_rect

    def paintEvent(self, event):
        self._paint_pending = False

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._mode == "black":
            painter.end()
            return

        # ── Resolve image to draw ─────────────────────────────────────────
        if self._mode == "video":
            if self._video_frame is not None:
                img = self._video_frame.toImage()
                if not img.isNull():
                    if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
                        img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                    self._video_image = img
                self._video_frame = None
            img_to_draw = self._video_image
        else:
            img_to_draw = self._static_image

        if img_to_draw is None or img_to_draw.isNull():
            painter.end()
            return

        # ── Base fit-to-widget rect ───────────────────────────────────────
        base = self._ensure_dst_rect(img_to_draw.width(), img_to_draw.height())

        # ── Apply zoom/pan transform (image mode only) ────────────────────
        transform = self._image_transform.current
        if self._mode == "image" and (
            abs(transform.zoom - 1.0) > 0.001
            or abs(transform.norm_x) > 0.0001
            or abs(transform.norm_y) > 0.0001
        ):
            ww = self.width()
            wh = self.height()
            cx = base.x() + base.width()  / 2.0
            cy = base.y() + base.height() / 2.0
            nw = base.width()  * transform.zoom
            nh = base.height() * transform.zoom
            ox = transform.norm_x * ww
            oy = transform.norm_y * wh
            dst = QRectF(cx - nw / 2.0 + ox, cy - nh / 2.0 + oy, nw, nh)
        else:
            dst = base

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(dst, img_to_draw)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cached_widget_size = QSize()


class BaseProjectionView(QWidget):
    """
    Shared content surface for both the on-monitor :class:`ProjectionWindow`
    and the on-screen :class:`FloatingPreviewWindow`.

    This base owns the 4-page ``QStackedWidget`` and **all** content
    behaviour — media/video, circular timer, yearly text and custom idle
    media. Media changes are deliberately immediate until the native GPU
    transition owns them; timer and idle-page animations remain local here.
    Subclasses are responsible only for *window-level* chrome
    (fullscreen placement vs. resizable 16:9 floating frame) and must call
    :meth:`_build_projection_stack` exactly once from their ``__init__``.

    Page indices (named below; never reorder — restore/replay logic depends on
    the layout being identical across both surfaces):

        0  media / image     (:class:`VideoDisplayWidget`)
        1  circular timer
        2  yearly text       (default idle screen)
        3  custom idle media (image or looping video)
    """

    # Named page indices so call sites read intent instead of magic numbers.
    _PAGE_MEDIA      = 0
    _PAGE_TIMER      = 1
    _PAGE_YEARLY     = 2
    _PAGE_IDLE_MEDIA = 3

    _YEARLY_TIMER_EXIT_FADE_DURATION_MS = 200
    _YEARLY_FADE_IN_DURATION_MS = 500

    def _build_projection_stack(self, layout) -> None:
        """Create the page stack, opacity effects, animations and state flags.

        Call once from a subclass ``__init__`` *after* ``super().__init__`` and
        after the subclass has created ``layout`` (a ``QVBoxLayout`` already set
        on ``self``).  Adds the stack to ``layout`` and selects the yearly-text
        idle page as the initial view.
        """
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: black;")
        layout.addWidget(self._stack)

        # Page 0 — media / image
        self._media_host = QWidget()
        media_layout = QStackedLayout(self._media_host)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.display_label = VideoDisplayWidget()
        self.native_video_surface = NativeVideoSurface()
        media_layout.addWidget(self.display_label)
        media_layout.addWidget(self.native_video_surface)
        self._stack.addWidget(self._media_host)   # index 0

        # Page 1 — live circular timer
        self._proj_timer = CircularTimerWidget()
        self._stack.addWidget(self._proj_timer)     # index 1

        # Opacity / fade effect on the timer widget
        self._timer_opacity = QGraphicsOpacityEffect(self._proj_timer)
        self._timer_opacity.setOpacity(1.0)
        self._proj_timer.setGraphicsEffect(self._timer_opacity)
        self._timer_anim = QPropertyAnimation(self._timer_opacity, b"opacity")
        self._timer_anim.setDuration(500)
        self._timer_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        # Page 2 — yearly text (idle screen)
        self._yearly_widget = YearlyTextWidget(self._font_manager)
        self._stack.addWidget(self._yearly_widget)  # index 2

        # Opacity / fade effect on the yearly text widget
        self._yearly_opacity = QGraphicsOpacityEffect(self._yearly_widget)
        self._yearly_opacity.setOpacity(1.0)
        self._yearly_widget.setGraphicsEffect(self._yearly_opacity)
        self._yearly_anim = QPropertyAnimation(self._yearly_opacity, b"opacity")
        self._yearly_anim.setDuration(self._YEARLY_FADE_IN_DURATION_MS)
        self._yearly_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._yearly_timer_exit_pending = False
        self._yearly_anim.finished.connect(self._on_yearly_animation_finished)

        # Page 3 — custom idle media (image or looping video)
        self._idle_media_widget = IdleMediaWidget()
        self._stack.addWidget(self._idle_media_widget)  # index 3

        self._idle_media_opacity = QGraphicsOpacityEffect(self._idle_media_widget)
        self._idle_media_opacity.setOpacity(1.0)
        self._idle_media_widget.setGraphicsEffect(self._idle_media_opacity)
        self._idle_media_anim = QPropertyAnimation(self._idle_media_opacity, b"opacity")
        self._idle_media_anim.setDuration(500)
        self._idle_media_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        # Explicit state flag — never rely on currentIndex for logic
        self._is_showing_media: bool = False
        self._has_idle_media: bool = False   # True when custom idle is loaded
        self._timer_presentation: MediaCountdownPresentation | None = None

        # Guard: only True while a *video* (not audio) is expected.
        # Set to True only by begin_video() / update_frame() explicitly called
        # for video content.  Set to False by clear(), show_image* and show_timer.
        # Prevents residual pipeline frames from a just-
        # stopped video from being painted after clear() is called.
        self._accept_video_frames: bool = False
        self._native_output_active: bool = False
        self._current_pixmap: QPixmap | None = None

        self._stack.setCurrentIndex(self._PAGE_YEARLY)  # start on yearly text page

    # ── Yearly text API ───────────────────────────────────────────────────

    def set_yearly_text(self, quote: str, reference: str, api_code: str = "") -> None:
        """Update the yearly text shown when idle."""
        self._yearly_widget.set_text(quote, reference, api_code)

    # ── Timer API ─────────────────────────────────────────────────────────

    def show_timer(
        self,
        remaining: int,
        total: int,
        presentation: MediaCountdownPresentation,
    ) -> None:
        """Display the countdown using the selected media-window presentation."""
        self._cancel_pending_timer_exit()
        self._stop_all_anims()
        self._clear_timer_presentation()
        self._timer_presentation = presentation
        self._accept_video_frames = False
        self.set_native_output_active(False)
        self._is_showing_media = False

        if presentation is MediaCountdownPresentation.YEARLY_TEXT:
            self._yearly_widget.set_countdown(remaining, total)
            self._yearly_opacity.setOpacity(0.0)
            self._stack.setCurrentIndex(self._PAGE_YEARLY)
            self._start_yearly_fade(
                start=0.0,
                end=1.0,
                duration_ms=self._YEARLY_FADE_IN_DURATION_MS,
            )
        else:
            self._proj_timer.update_data(remaining, total)
            self._timer_opacity.setOpacity(0.0)
            self._stack.setCurrentIndex(self._PAGE_TIMER)
            self._timer_anim.setStartValue(0.0)
            self._timer_anim.setEndValue(1.0)
            self._timer_anim.start()

    def update_timer(self, remaining: int, total: int) -> None:
        """Update countdown (called every second while timer mode is active)."""
        if (
            self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT
            and self._stack.currentIndex() == self._PAGE_YEARLY
        ):
            self._yearly_widget.set_countdown(remaining, total)
        elif (
            self._timer_presentation is MediaCountdownPresentation.CIRCULAR
            and self._stack.currentIndex() == self._PAGE_TIMER
        ):
            self._proj_timer.update_data(remaining, total)

    def set_timer_blink(self, on: bool) -> None:
        if (
            self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT
            and self._stack.currentIndex() == self._PAGE_YEARLY
        ):
            self._yearly_widget.set_countdown_blink(on)
        elif (
            self._timer_presentation is MediaCountdownPresentation.CIRCULAR
            and self._stack.currentIndex() == self._PAGE_TIMER
        ):
            self._proj_timer.set_blink(on)

    def _clear_timer_presentation(self) -> None:
        if self._timer_presentation is None:
            return
        self._timer_presentation = None
        self._yearly_widget.clear_countdown()
        self._proj_timer.set_blink(False)

    # ── Media / image API ─────────────────────────────────────────────────

    def begin_video(self) -> None:
        """Arm the video-frame gate.

        Must be called once per video playback session, *after* clear() and
        *before* the first frame arrives from the media pipeline.  This is the
        single authoritative place that re-enables update_frame(); every other
        path (clear, show_image* or show_timer) disables it.
        """
        self._cancel_pending_timer_exit()
        self._clear_timer_presentation()
        self._accept_video_frames = True

    def set_native_output_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._native_output_active:
            return
        self._native_output_active = active
        self.native_video_surface.setVisible(active)
        if active:
            self.display_label.clear()
            self._stop_all_anims()
            self._is_showing_media = True
            self._stack.setCurrentIndex(self._PAGE_MEDIA)
            self.native_video_surface.raise_()

    @Slot(QVideoFrame)
    def update_frame(self, frame: QVideoFrame) -> None:
        """Receive a video frame and display it."""
        # Reject frames when we are not expecting video — this is the primary
        # defence against residual pipeline frames arriving after clear() is
        # called (e.g. when switching from video to audio).
        if not self._accept_video_frames:
            return
        # Pass the raw QVideoFrame — VideoDisplayWidget handles throttle + conversion
        if not frame.isValid():
            return
        if self._native_output_active:
            return
        self.display_label.set_video_frame(frame)

        if not self._is_showing_media:
            self._stop_all_anims()
            self._is_showing_media = True
            self._stack.setCurrentIndex(self._PAGE_MEDIA)

    def show_image_from_url_data(
        self,
        data: bytes,
        *,
        initial_transform: ImageTransform | None = None,
    ) -> None:
        """Display an image from raw bytes."""
        image = QImage()
        image.loadFromData(data)
        if not image.isNull():
            self._show_image(image, initial_transform=initial_transform)

    def show_image_from_pixmap(
        self,
        pixmap: QPixmap,
        *,
        initial_transform: ImageTransform | None = None,
    ) -> None:
        self._show_image(pixmap.toImage(), initial_transform=initial_transform)

    def show_image_from_qimage(
        self,
        image: QImage,
        *,
        cache_pixmap: bool = True,
        initial_transform: ImageTransform | None = None,
    ) -> None:
        self._show_image(
            image,
            cache_pixmap=cache_pixmap,
            initial_transform=initial_transform,
        )

    def _show_image(
        self,
        image: QImage,
        *,
        cache_pixmap: bool = True,
        initial_transform: ImageTransform | None = None,
    ) -> None:
        """Route a static QImage to the display widget."""
        # Static image — video pipeline must not overwrite it.
        self._cancel_pending_timer_exit()
        self._clear_timer_presentation()
        self._accept_video_frames = False
        self.set_native_output_active(False)
        self._current_pixmap = QPixmap.fromImage(image) if cache_pixmap else None
        self.display_label.set_image(image, initial_transform=initial_transform)

        if not self._is_showing_media:
            self._stop_all_anims()
            self._is_showing_media = True
            self._stack.setCurrentIndex(self._PAGE_MEDIA)
        else:
            self._stack.setCurrentIndex(self._PAGE_MEDIA)

    def set_image_transform(self, zoom: float, norm_x: float, norm_y: float,
                            *, animate: bool = True) -> None:
        """Forward a zoom/pan transform to whichever widget is currently visible.

        ``animate=False`` snaps instantly — used to replay the active transform
        onto a newly created surface so it matches the others immediately.
        """
        if self._stack.currentIndex() == self._PAGE_MEDIA:
            self.display_label.set_image_transform(zoom, norm_x, norm_y, animate=animate)

    def reset_image_transform_instant(self) -> None:
        """Snap the image display to identity before loading a new projection."""
        self.display_label.reset_transform_instant()

    # ── Idle / clear transitions ──────────────────────────────────────────

    def clear(self) -> None:
        """Return media to the active idle page without a CPU composited fade."""
        # Immediately stop accepting video frames — this is the earliest possible
        # point to cut off the pipeline, before any async frames already queued
        # in the Qt event loop can reach update_frame().
        self._accept_video_frames = False
        self.set_native_output_active(False)
        if self._yearly_timer_exit_pending:
            return
        if (
            self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT
            and self._stack.currentIndex() == self._PAGE_YEARLY
        ):
            self._stop_all_anims()
            self._current_pixmap = None
            self._yearly_timer_exit_pending = True
            self._start_yearly_fade(
                start=self._yearly_opacity.opacity(),
                end=0.0,
                duration_ms=self._YEARLY_TIMER_EXIT_FADE_DURATION_MS,
            )
            return

        self._clear_timer_presentation()

        # Already on the correct idle page and nothing animating — do nothing
        # (avoids flicker on audio-only clips).
        idle_page = self._PAGE_IDLE_MEDIA if self._has_idle_media else self._PAGE_YEARLY
        already_idle = (self._stack.currentIndex() == idle_page and not self._is_showing_media)
        if already_idle:
            return

        self._stop_all_anims()
        self._current_pixmap = None

        if self._is_showing_media:
            self._is_showing_media = False
            self.display_label.clear()
            self._switch_to_idle_immediately()
        else:
            # Coming from timer — switch to idle with fade-in
            self._switch_to_idle_with_fade()

    def _cancel_pending_timer_exit(self) -> None:
        if not self._yearly_timer_exit_pending:
            return
        self._yearly_anim.stop()
        self._yearly_timer_exit_pending = False
        self._yearly_opacity.setOpacity(1.0)

    def _start_yearly_fade(self, *, start: float, end: float, duration_ms: int) -> None:
        self._yearly_anim.setDuration(duration_ms)
        self._yearly_anim.setStartValue(start)
        self._yearly_anim.setEndValue(end)
        self._yearly_anim.start()

    def _on_yearly_animation_finished(self) -> None:
        if not self._yearly_timer_exit_pending:
            return
        self._yearly_timer_exit_pending = False
        self._clear_timer_presentation()
        self._switch_to_idle_with_fade()

    def _switch_to_idle_with_fade(self) -> None:
        """Switch to the idle screen (custom media page 3 if set, else yeartext page 2)."""
        if self._has_idle_media:
            self._idle_media_opacity.setOpacity(0.0)
            self._stack.setCurrentIndex(self._PAGE_IDLE_MEDIA)
            self._idle_media_anim.setStartValue(0.0)
            self._idle_media_anim.setEndValue(1.0)
            self._idle_media_anim.start()
        else:
            self._yearly_opacity.setOpacity(0.0)
            self._stack.setCurrentIndex(self._PAGE_YEARLY)
            self._start_yearly_fade(
                start=0.0,
                end=1.0,
                duration_ms=self._YEARLY_FADE_IN_DURATION_MS,
            )

    def _switch_to_idle_immediately(self) -> None:
        if self._has_idle_media:
            self._idle_media_opacity.setOpacity(1.0)
            self._stack.setCurrentIndex(self._PAGE_IDLE_MEDIA)
        else:
            self._yearly_opacity.setOpacity(1.0)
            self._stack.setCurrentIndex(self._PAGE_YEARLY)

    def _stop_all_anims(self) -> None:
        """Stop all animations and disconnect callbacks safely."""
        for anim in (
            self._yearly_anim,
            self._timer_anim,
            self._idle_media_anim,
        ):
            anim.stop()

    # ── Custom idle media API ─────────────────────────────────────────────

    def set_idle_active(self) -> None:
        """Mark that a shared custom idle is active for this surface.

        Frames are decoded by the shared IdleMediaSource and delivered via
        update_idle_image(); this only flips the surface into idle-media mode and,
        if currently on the plain yeartext idle page, fades over to it.
        """
        self._has_idle_media = True
        if (
            self._timer_presentation is None
            and self._stack.currentIndex() == self._PAGE_YEARLY
            and not self._is_showing_media
        ):
            self._stop_all_anims()
            self._switch_to_idle_with_fade()

    def clear_idle(self) -> None:
        """Stop showing the custom idle and return to the yeartext screen."""
        self._has_idle_media = False
        self._idle_media_widget.clear()
        if self._stack.currentIndex() == self._PAGE_IDLE_MEDIA and not self._is_showing_media:
            self._stop_all_anims()
            self._switch_to_idle_with_fade()

    def update_idle_image(self, image: QImage) -> None:
        """Paint the latest idle frame from the shared source.

        No-op unless a custom idle is currently active for this surface, so the
        controller can fan every frame out to all surfaces unconditionally.
        """
        if self._has_idle_media:
            self._idle_media_widget.set_image(image)

    def bind_idle_visibility(self, slot) -> None:
        """Connect a callback(bool) fired when this surface's custom-idle page is
        shown/hidden, so the shared source can pause decoding while no surface is
        displaying the idle video."""
        self._idle_media_widget.visibility_changed.connect(slot)

    def is_idle_visible(self) -> bool:
        """True when the custom-idle page is currently visible on this surface."""
        return self._idle_media_widget.isVisible()


class ProjectionWindow(BaseProjectionView):
    """Fullscreen window displayed on a secondary monitor."""

    def __init__(
        self,
        screen,
        monitor_index: int,
        font_manager: FontManager,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self._font_manager = font_manager
        self.monitor_index = monitor_index

        # Window setup
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet("background-color: black;")

        # Place on specific screen
        self.setScreen(screen)
        self.move(screen.geometry().topLeft())
        self.resize(screen.geometry().size())

        # ── Content stack (shared with FloatingPreviewWindow) ─────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._build_projection_stack(layout)
        self.setLayout(layout)

        # ── Fade-in on open ───────────────────────────────────────────────
        self.setWindowOpacity(0.0)
        self.showFullScreen()
        self._open_anim = QPropertyAnimation(self, b"windowOpacity")
        self._open_anim.setDuration(480)
        self._open_anim.setStartValue(0.0)
        self._open_anim.setEndValue(1.0)
        self._open_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Delay slightly so the window is actually rendered before animating
        QTimer.singleShot(60, self._open_anim.start)

        # ── Auto-refit when the screen's resolution/position changes ─────
        # This covers cases like resolution switches while the window is live.
        screen.geometryChanged.connect(self._on_screen_geometry_changed)

        # Exclude this window from Windows Aero Peek.
        # Without this, hovering over any app's thumbnail in the taskbar causes
        # Windows to hide all non-targeted windows (including this one on the
        # secondary monitor), making the projection screen go blank until the
        # user clicks or moves the mouse away from the thumbnail preview.
        exclude_from_aero_peek(int(self.winId()))

    @Slot()
    def _on_screen_geometry_changed(self):
        """Re-snap to screen when resolution or position changes (e.g. user changes
        resolution in display settings while the app is running).
        Uses a 300 ms delay — Windows needs ~200 ms to fully settle the new
        geometry in the DWM before we can reliably read and apply it."""
        QTimer.singleShot(300, self.refit_to_screen)

    def refit_to_screen(self, screen=None):
        """
        Re-snap this window to its associated screen's current geometry.

        Only runs the showNormal/showFullScreen cycle when the geometry has
        actually changed — prevents disrupting content unnecessarily.

        After the cycle the active stack page gets an explicit update() so
        VideoDisplayWidget / YearlyTextWidget etc. repaint correctly on Windows
        (which does not always issue a spontaneous expose event after fullscreen
        is re-applied).
        """
        if screen is not None:
            self.setScreen(screen)
        target_screen = self.screen()
        if target_screen is None:
            return
        geo = target_screen.geometry()

        # No change → nothing to do. Avoids the showNormal→showFullScreen
        # cycle (which drops content) when only a different monitor changed.
        if (self.isFullScreen()
                and self.pos() == geo.topLeft()
                and self.size() == geo.size()):
            return

        # Preserve opacity so any running fade animation is not disrupted
        saved_opacity = self.windowOpacity()

        # Hide for the single frame the window is in "normal" state
        self.setWindowOpacity(0.0)
        self.showNormal()
        self.move(geo.topLeft())
        self.resize(geo.size())
        self.showFullScreen()

        def _restore():
            # Restore opacity
            self.setWindowOpacity(saved_opacity)
            # Force all stack pages to repaint — without this Windows sometimes
            # leaves the projection black after the fullscreen cycle even though
            # the widget state (image data, yearly text, etc.) is intact.
            for i in range(self._stack.count()):
                self._stack.widget(i).update()
            # Re-apply Aero Peek exclusion — the HWND may have been recreated
            exclude_from_aero_peek(int(self.winId()))

        QTimer.singleShot(60, _restore)

    def fade_out_and_close(self):
        """Animate opacity to 0 then close this window (for individual monitor removal)."""
        self._stop_all_anims()
        self._accept_video_frames = False
        self._close_anim = QPropertyAnimation(self, b"windowOpacity")
        self._close_anim.setDuration(380)
        self._close_anim.setStartValue(self.windowOpacity())
        self._close_anim.setEndValue(0.0)
        self._close_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._close_anim.finished.connect(self.close)
        self._close_anim.start()


# ── Floating Preview Window ───────────────────────────────────────────────────

class FloatingPreviewWindow(BaseProjectionView):
    """
    Frameless, resizable 16:9 preview window displayed on the primary monitor.

    Design goals
    ────────────
    • Visible to the OS window manager and screen-capture tools (Zoom, Teams,
      OBS…) because it uses Qt.WindowType.Window — NOT Tool/Popup, which are
      excluded from capture lists on every major platform.
    • Fixed English title "Solin Media Preview" so external tools can identify
      it reliably by name in their window-picker dialogs.
    • No titlebar; left-button drag on the content area moves the window.
    • Resizable via all edges and corners; 16:9 aspect ratio enforced at all
      times during resize.
    • Default geometry: 30 % of primary monitor height, positioned at (50, 50)
      relative to the primary screen's top-left corner.

    Zoom-break (HWND change) — how it works
    ────────────────────────────────────────
    Screen-sharing tools (Zoom, Teams, OBS…) bind to a window by its native
    HWND.  A simple hide()/show() cycle does NOT change the HWND, so those
    tools keep capturing.  To force them to drop the capture reference the
    window must be destroyed — which creates a new HWND on recreation.

    trigger_zoom_break() orchestrates this:
      1. Saves own geometry.
      2. Hides the window (imperceptible to the user).
      3. After _ZOOM_BREAK_MS ms emits respawn_requested(QRect) — carrying
         the saved geometry — then closes and schedules self for deletion.
      4. MainWindow's _on_floating_respawn() slot handles recreating a fresh
         FloatingPreviewWindow at the same position/size, completely silently
         (no monitor-manager popup is triggered).
    """

    # Emitted just before self-destruction during a zoom-break.
    # MainWindow recreates the window at the carried geometry with a fresh HWND.
    respawn_requested = Signal(QRect)

    WINDOW_TITLE         = "Solin Media Preview"
    _DEFAULT_HEIGHT_RATIO = 0.30    # fraction of primary screen height
    _ASPECT_RATIO         = 16 / 9
    _RESIZE_MARGIN        = 8       # px within which edges trigger resize
    _MIN_HEIGHT           = 90      # absolute minimum height (keeps 16:9 sane)
    _ZOOM_BREAK_MS        = 80      # ms to stay hidden for Zoom-break

    def __init__(self,
                 font_manager: FontManager,
                 yearly_text_quote: str = "",
                 yearly_text_ref:   str = "",
                 api_code:          str = ""):
        super().__init__(None)      # no Qt parent → proper top-level window
        self._font_manager = font_manager

        # ── Window flags ──────────────────────────────────────────────────
        # Qt.WindowType.Window  → registered with the OS window manager,
        #                          appears in Alt-Tab, taskbar, and Zoom's
        #                          "Share Window" picker on all platforms.
        # FramelessWindowHint   → no title bar / OS chrome.
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setStyleSheet("background-color: black;")
        self.setMinimumSize(int(self._MIN_HEIGHT * 16 / 9), self._MIN_HEIGHT)

        # ── Initial geometry ──────────────────────────────────────────────
        primary    = QGuiApplication.primaryScreen()
        screen_geo = primary.availableGeometry()
        init_h     = max(self._MIN_HEIGHT,
                         int(screen_geo.height() * self._DEFAULT_HEIGHT_RATIO))
        init_w     = round(init_h * 16 / 9)
        self.move(screen_geo.topLeft() + QPoint(50, 50))
        self.resize(init_w, init_h)

        # ── Resize / drag interaction state ──────────────────────────────
        self._resizing:            bool        = False
        self._resize_dir:          tuple       = (False, False, False, False)
        self._resize_origin_pos:   QPoint      = QPoint()
        self._resize_origin_geom               = self.geometry()
        self._drag_pos:            QPoint | None = None
        self._normal_geometry_before_fullscreen: QRect = QRect()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # ── Content stack (shared with ProjectionWindow) ──────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._build_projection_stack(layout)

        if yearly_text_quote:
            self._yearly_widget.set_text(yearly_text_quote, yearly_text_ref, api_code)

        # ── Child mouse-tracking for resize-cursor feedback ───────────────
        # The QStackedWidget and all its page-widgets cover the entire window
        # surface and intercept mouse events before they reach the parent.
        # Installing ourselves as an event filter on every descendant lets us
        # observe MouseMove at the parent level and update the cursor correctly
        # even when the pointer is over a child widget.
        self._install_child_tracking(self._stack)
        # The native scene presenter belongs to the media-engine process and
        # cannot reliably forward hover through its child HWND hierarchy.  A
        # transparent app-process input plane keeps Qt as the sole owner of
        # move and resize interaction while the presenter only renders video.
        self.native_video_surface.set_input_target(self)
        
        # ── Keep-alive (Anti-congelamento para Zoom/OBS) ──────────────────
        # Cria um pixel 1x1 no topo esquerdo (0,0) que flutua sobre a UI
        self._keep_alive_pixel = QWidget(self)
        self._keep_alive_pixel.setGeometry(0, 0, 1, 1)
        self._keep_alive_pixel.raise_()  # Garante que fica por cima do stack
        
        self._keep_alive_state = False
        self._keep_alive_timer = QTimer(self)
        self._keep_alive_timer.timeout.connect(self._toggle_keep_alive)
        self._keep_alive_timer.start(1000)  # Pulso a cada 1 segundo

        # ── Zoom-break timer ──────────────────────────────────────────────
        self._zoom_break_timer = QTimer(self)
        self._zoom_break_timer.setSingleShot(True)
        self._zoom_break_timer.setInterval(self._ZOOM_BREAK_MS)
        self._zoom_break_timer.timeout.connect(self._on_zoom_break_done)
        # Saved by trigger_zoom_break(); carried by respawn_requested signal
        self._respawn_geometry: QRect = QRect()

        # ── Fade-in on open ───────────────────────────────────────────────
        self.setWindowOpacity(0.0)
        self.show()
        self._open_anim = QPropertyAnimation(self, b"windowOpacity")
        self._open_anim.setDuration(380)
        self._open_anim.setStartValue(0.0)
        self._open_anim.setEndValue(1.0)
        self._open_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        QTimer.singleShot(40, self._open_anim.start)
        QTimer.singleShot(0, self.setFocus)

    # ── Child mouse-tracking helpers ─────────────────────────────────────

    def _install_child_tracking(self, widget: QWidget) -> None:
        """Recursively enable mouse tracking and install this window as event
        filter on *widget* and every QWidget descendant.

        This is necessary because child widgets (QStackedWidget, VideoDisplayWidget,
        YearlyTextWidget, …) cover the entire window surface.  Qt routes
        MouseMove events to the deepest widget under the cursor, so the parent's
        mouseMoveEvent never fires while a child is hovered.  Intercepting via
        eventFilter lets us still read the position relative to the parent and
        update the resize cursor appropriately.

        Called once from __init__ after all child widgets are created; safe to
        call again if new children are added dynamically.
        """
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.children():
            if isinstance(child, QWidget):
                self._install_child_tracking(child)

    def eventFilter(self, obj: QWidget, event: QEvent) -> bool:  # type: ignore[override]
        """Observe MouseMove on child widgets to drive the resize cursor.

        We never consume the event (always return False) — we only peek at it
        to call setCursor() on the parent window.  Returning False passes the
        event on to *obj* as normal.
        """
        if event.type() == QEvent.Type.MouseButtonDblClick and isinstance(obj, QWidget):
            if self._is_left_mouse_event(event):
                self.toggle_fullscreen()
                return True
        if (
            event.type() == QEvent.Type.KeyPress
            and self.isFullScreen()
            and self._is_escape_key_event(event)
        ):
            self.exit_fullscreen()
            return True
        if event.type() == QEvent.Type.MouseMove and isinstance(obj, QWidget):
            try:
                if self.isFullScreen():
                    self._set_interaction_cursor(self._ARROW)
                    return False
                global_pos = obj.mapToGlobal(event.position().toPoint())
                local_pos  = self.mapFromGlobal(global_pos)
                self._set_interaction_cursor(
                    self._cursor_for_edges(self._edge_hits(local_pos))
                )
            except Exception:  # noqa: BLE001 - Qt event-filter boundary
                log_ignored_exception(__name__, "Could not update projection resize cursor")
        return False  # do NOT consume — let the event propagate normally

    @staticmethod
    def _is_left_mouse_event(event: QEvent) -> bool:
        return (
            isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        )

    @staticmethod
    def _is_escape_key_event(event: QEvent) -> bool:
        return (
            isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        )

    def toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode for the windowed preview."""
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        if self.isFullScreen():
            return
        self._normal_geometry_before_fullscreen = self.geometry()
        self._resize_dir = (False, False, False, False)
        self._drag_pos = None
        self._set_interaction_cursor(self._ARROW)
        self.showFullScreen()
        self.setFocus()
        self._refresh_after_window_state_change()

    def exit_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        restore_geometry = QRect(self._normal_geometry_before_fullscreen)
        self._resize_dir = (False, False, False, False)
        self._drag_pos = None
        self.showNormal()
        if restore_geometry.isValid() and not restore_geometry.isEmpty():
            self.setGeometry(restore_geometry)
        self._set_interaction_cursor(self._ARROW)
        self.setFocus()
        self._refresh_after_window_state_change()

    def _refresh_after_window_state_change(self) -> None:
        """Repaint projection pages after fullscreen/windowed transitions."""
        for i in range(self._stack.count()):
            self._stack.widget(i).update()
        if sys.platform == "win32":
            QTimer.singleShot(60, lambda: exclude_from_aero_peek(int(self.winId())))
    
    def _toggle_keep_alive(self) -> None:
        """
        Força um redesenho mínimo na janela para enganar a otimização do DWM.
        Alterna a cor de um pixel 1x1 entre preto e '#010101'.
        Isso impede que ferramentas de captura (Zoom, Teams) congelem a tela
        quando uma imagem estática estiver sendo exibida.
        """
        self._keep_alive_state = not self._keep_alive_state
        color = "#0101010F" if self._keep_alive_state else "#0000002D"
        self._keep_alive_pixel.setStyleSheet(f"background-color: {color};")

    # ── Zoom-break ────────────────────────────────────────────────────────

    def trigger_zoom_break(self) -> None:
        """
        Destroys this window and requests recreation via respawn_requested.

        Screen-sharing tools (Zoom, Teams…) bind captures to the native HWND.
        A simple hide()/show() cycle keeps the same HWND — tools keep
        capturing.  Full destruction forces a new HWND on recreation, causing
        every sharing tool to drop its capture reference automatically.

        Flow:
          1. Saves own geometry.
          2. Hides window (_ZOOM_BREAK_MS gap is imperceptible to users).
          3. On timer: emits respawn_requested(saved_geo) then close()/deleteLater().
          4. MainWindow._on_floating_respawn() silently recreates a fresh window
             at saved_geo — no monitor-manager popup is triggered.

        Safe to re-call while a break is in progress (timer is re-armed).
        Must NOT be called on media-to-media transitions.
        """
        if self._zoom_break_timer.isActive():
            self._zoom_break_timer.stop()
        # Save geometry before hiding (some OSes report wrong geometry for
        # hidden windows)
        self._respawn_geometry = self.geometry()
        self.hide()
        self._zoom_break_timer.start()

    def _on_zoom_break_done(self) -> None:
        """Emit respawn signal then self-destruct to force an HWND change."""
        # Emit first — the signal carries the saved geometry to the receiver.
        # After close() the Qt object lifecycle ends; emit must happen before.
        self.respawn_requested.emit(self._respawn_geometry)
        self.close()
        self.deleteLater()

    # ── Resize: enforce 16:9 at all times ────────────────────────────────

    def resizeEvent(self, event) -> None:                   # type: ignore[override]
        """Keep the window exactly 16:9 regardless of which edge was dragged."""
        if self.isFullScreen() or self._resizing:
            super().resizeEvent(event)
            return
        new_w = event.size().width()
        new_h = event.size().height()
        old_w = event.oldSize().width()
        old_h = event.oldSize().height()
        if old_w > 0 and old_h > 0:
            d_w = abs(new_w - old_w)
            d_h = abs(new_h - old_h)
            if d_w >= d_h:
                # Width changed more — snap height
                target_h = max(self._MIN_HEIGHT, round(new_w * 9 / 16))
                if target_h != new_h:
                    self._resizing = True
                    self.resize(new_w, target_h)
                    self._resizing = False
                    return
            else:
                # Height changed more — snap width
                target_w = max(int(self._MIN_HEIGHT * 16 / 9),
                               round(new_h * 16 / 9))
                if target_w != new_w:
                    self._resizing = True
                    self.resize(target_w, new_h)
                    self._resizing = False
                    return
        super().resizeEvent(event)

    # ── Mouse: drag-to-move and edge/corner resize ────────────────────────

    _DIAG_FWD  = Qt.CursorShape.SizeFDiagCursor   # ↘ / ↖
    _DIAG_BACK = Qt.CursorShape.SizeBDiagCursor   # ↗ / ↙
    _HOR       = Qt.CursorShape.SizeHorCursor
    _VER       = Qt.CursorShape.SizeVerCursor
    _ARROW     = Qt.CursorShape.ArrowCursor

    def _edge_hits(self, pos: QPoint) -> tuple:
        """Return (left, top, right, bottom) bool — True if pos is on that edge."""
        m = self._RESIZE_MARGIN
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        return x < m, y < m, x > w - m, y > h - m

    def _cursor_for_edges(self, edges: tuple) -> Qt.CursorShape:
        l, t, r, b = edges
        if (l and t) or (r and b): return self._DIAG_FWD
        if (r and t) or (l and b): return self._DIAG_BACK
        if l or r:                 return self._HOR
        if t or b:                 return self._VER
        return self._ARROW

    def _set_interaction_cursor(self, cursor: Qt.CursorShape) -> None:
        """Keep the frameless host and its native presenter cursor in sync."""
        self.setCursor(cursor)
        self.native_video_surface.set_interaction_cursor(cursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.isFullScreen():
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos   = event.position().toPoint()
        edges = self._edge_hits(pos)
        if any(edges):
            self._resize_dir         = edges
            self._drag_pos           = None
            self._resize_origin_pos  = event.globalPosition().toPoint()
            self._resize_origin_geom = self.geometry()
        else:
            self._resize_dir = (False, False, False, False)
            self._drag_pos   = (event.globalPosition().toPoint()
                                - self.frameGeometry().topLeft())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isFullScreen():
            self._set_interaction_cursor(self._ARROW)
            event.accept()
            return
        pos = event.position().toPoint()
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            # Hover — update resize cursor
            self._set_interaction_cursor(
                self._cursor_for_edges(self._edge_hits(pos))
            )
            super().mouseMoveEvent(event)
            return

        if any(self._resize_dir):
            self._do_resize(event.globalPosition().toPoint())
        elif self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._resize_dir = (False, False, False, False)
            self._drag_pos   = None
            if not self.isFullScreen():
                self._set_interaction_cursor(
                    self._cursor_for_edges(
                        self._edge_hits(event.position().toPoint())
                    )
                )
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_fullscreen()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.exit_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def _do_resize(self, global_pos: QPoint) -> None:
        """Apply a resize drag while enforcing 16:9 and minimum size."""
        l, t, r, b = self._resize_dir
        delta = global_pos - self._resize_origin_pos
        orig  = self._resize_origin_geom

        aspect = self._ASPECT_RATIO
        new_x = orig.x()
        new_y = orig.y()
        new_w = orig.width()
        new_h = orig.height()
        min_w = round(self._MIN_HEIGHT * aspect)
        min_h = self._MIN_HEIGHT
        anchor_right = orig.x() + orig.width()
        anchor_bottom = orig.y() + orig.height()

        only_horiz = (l or r) and not (t or b)
        only_vert  = (t or b) and not (l or r)
        corner     = (l or r) and (t or b)

        if only_horiz:
            signed_dx = -delta.x() if l else delta.x()
            new_w = max(min_w, orig.width() + signed_dx)
            new_h = max(min_h, round(new_w / aspect))
            if l:
                new_x = anchor_right - new_w
        elif only_vert:
            signed_dy = -delta.y() if t else delta.y()
            new_h = max(min_h, orig.height() + signed_dy)
            new_w = max(min_w, round(new_h * aspect))
            if t:
                new_y = anchor_bottom - new_h
        elif corner:
            # Project the mouse movement onto the locked 16:9 diagonal. This
            # keeps corner resizing smooth instead of switching master axes
            # whenever horizontal and vertical deltas overtake each other.
            signed_dx = -delta.x() if l else delta.x()
            signed_dy = -delta.y() if t else delta.y()
            delta_h = ((aspect * signed_dx) + signed_dy) / ((aspect * aspect) + 1)
            new_h = max(min_h, round(orig.height() + delta_h))
            new_w = max(min_w, round(new_h * aspect))

            if l:
                new_x = anchor_right - new_w
            if t:
                new_y = anchor_bottom - new_h

        self._resizing = True
        try:
            self.setGeometry(new_x, new_y, new_w, new_h)
        finally:
            self._resizing = False
