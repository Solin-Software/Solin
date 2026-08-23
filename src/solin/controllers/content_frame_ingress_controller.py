from __future__ import annotations

import logging
import threading
import time
from collections.abc import Buffer, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from solin.core.scenes.engine import FrameChannelDescriptor
from solin.core.scenes.frame_channel import (
    FrameChannelStaleFrameError,
    FrameChannelUnavailableError,
    SharedMemoryVideoFramePublisher,
)
from solin.core.scenes.model import VideoPixelFormat
from solin.core.projection.image_framing import (
    ImageTransform,
    normalize_image_transform,
)
from solin.core.projection.transform_animation import (
    PROJECTION_TRANSFORM_DURATION_SECONDS,
)


log = logging.getLogger(__name__)
_DEFAULT_MAXIMUM_FPS = 30
_DEFAULT_CANVAS_WIDTH = 1920
_DEFAULT_CANVAS_HEIGHT = 1080
_SHUTDOWN_BUDGET_SECONDS = 0.5
_TRANSFORM_DURATION_MS = round(PROJECTION_TRANSFORM_DURATION_SECONDS * 1_000)


@dataclass(frozen=True, slots=True)
class _ImageTransformRequest:
    transform: ImageTransform | None
    media_epoch: int
    canvas_width: int
    canvas_height: int
    animate: bool


class _FramePublisher(Protocol):
    @property
    def descriptor(self) -> FrameChannelDescriptor: ...

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int: ...

    def publish_planes(
        self,
        planes: tuple[Buffer, ...],
        *,
        plane_strides: tuple[int, ...],
        frame_width: int,
        frame_height: int,
        pixel_format: VideoPixelFormat,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int: ...

    def set_media_epoch(self, media_epoch: int) -> None: ...

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        *,
        media_epoch: int,
        canvas_width: int,
        canvas_height: int,
        animate: bool,
        duration_ms: int,
    ) -> None: ...

    def close(self) -> None: ...


class ContentFrameIngressController(QObject):
    """Coalesce Qt content frames into the native engine's bounded data plane."""

    descriptor_changed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        publisher_factory: Callable[[int, int], _FramePublisher] | None = None,
        maximum_fps: int = _DEFAULT_MAXIMUM_FPS,
        canvas_width: int = _DEFAULT_CANVAS_WIDTH,
        canvas_height: int = _DEFAULT_CANVAS_HEIGHT,
    ) -> None:
        super().__init__(parent)
        if isinstance(maximum_fps, bool) or not isinstance(maximum_fps, int):
            raise ValueError("Content ingress FPS must be an integer")
        if not 1 <= maximum_fps <= 60:
            raise ValueError("Content ingress FPS must be between 1 and 60")
        if isinstance(canvas_width, bool) or not isinstance(canvas_width, int):
            raise ValueError("Content ingress width must be an integer")
        if isinstance(canvas_height, bool) or not isinstance(canvas_height, int):
            raise ValueError("Content ingress height must be an integer")
        if canvas_width <= 0 or canvas_height <= 0:
            raise ValueError("Content ingress dimensions must be positive")
        self._publisher_factory = publisher_factory or SharedMemoryVideoFramePublisher
        self._canvas_size = QSize(canvas_width, canvas_height)
        self._minimum_interval = 1.0 / maximum_fps
        self._condition = threading.Condition()
        self._publisher_lock = threading.Lock()
        self._worker_stopped = threading.Event()
        self._pending_frame: tuple[QImage, int] | None = None
        self._pending_media_epoch: int | None = None
        self._pending_image_transform: _ImageTransformRequest | None = None
        self._retained_frame: tuple[QImage, int] | None = None
        self._media_epoch = 0
        self._image_transform = _ImageTransformRequest(
            transform=None,
            media_epoch=0,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            animate=False,
        )
        self._next_video_materialization_at = 0.0
        self._enabled = True
        self._closed = False
        self._publisher: _FramePublisher | None = self._create_publisher()
        self._worker = threading.Thread(
            target=self._run,
            name="solin-content-frame-ingress",
            daemon=True,
        )
        self._worker.start()

    @property
    def descriptor(self) -> FrameChannelDescriptor | None:
        with self._publisher_lock:
            publisher = self._publisher
            return publisher.descriptor if publisher is not None else None

    @property
    def enabled(self) -> bool:
        with self._condition:
            return self._enabled and not self._closed

    @Slot(bool)
    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._condition:
            if self._closed or self._enabled == enabled:
                return
            self._enabled = enabled
            self._next_video_materialization_at = 0.0
            self._pending_frame = (
                (QImage(self._retained_frame[0]), self._retained_frame[1])
                if enabled and self._retained_frame is not None
                else None
            )
            self._condition.notify_all()

    def set_media_epoch(self, media_epoch: int) -> None:
        if (
            isinstance(media_epoch, bool)
            or not isinstance(media_epoch, int)
            or media_epoch < 0
            or media_epoch > 2**64 - 1
        ):
            raise ValueError("Content media epoch must be a non-negative 64-bit integer")
        with self._condition:
            if self._closed or media_epoch <= self._media_epoch:
                return
            self._media_epoch = media_epoch
            self._pending_media_epoch = media_epoch
            if self._pending_frame is not None and self._pending_frame[1] < media_epoch:
                self._pending_frame = None
            self._condition.notify_all()

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        *,
        media_epoch: int,
        canvas_width: int,
        canvas_height: int,
        animate: bool,
    ) -> None:
        if (
            isinstance(media_epoch, bool)
            or not isinstance(media_epoch, int)
            or not 0 <= media_epoch <= 2**64 - 1
        ):
            raise ValueError("Content transform epoch must be a non-negative 64-bit integer")
        if (
            isinstance(canvas_width, bool)
            or not isinstance(canvas_width, int)
            or isinstance(canvas_height, bool)
            or not isinstance(canvas_height, int)
            or not 1 <= canvas_width <= self._canvas_size.width()
            or not 1 <= canvas_height <= self._canvas_size.height()
        ):
            raise ValueError("Content transform canvas exceeds the ingress capacity")
        request = _ImageTransformRequest(
            transform=(
                normalize_image_transform(transform) if transform is not None else None
            ),
            media_epoch=media_epoch,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            animate=bool(animate),
        )
        with self._condition:
            if self._closed or request == self._image_transform:
                return
            self._image_transform = request
            self._pending_image_transform = request
            self._condition.notify_all()

    def submit_frame(self, frame: object) -> None:
        if isinstance(frame, QVideoFrame):
            if not frame.isValid():
                return
            # Sampling must happen before QVideoFrame.toImage(). Qt video sinks
            # commonly deliver 60 fps while Program is 30 fps; converting the
            # half that the bounded worker will discard wastes a full GPU/CPU
            # surface readback and was a major part of projected-video CPU use.
            with self._condition:
                now = time.monotonic()
                self._retained_frame = None
                if self._closed or not self._enabled:
                    return
                next_deadline = _advance_video_deadline(
                    self._next_video_materialization_at,
                    now,
                    self._minimum_interval,
                )
                if next_deadline is None:
                    return
                self._next_video_materialization_at = next_deadline
                media_epoch = self._media_epoch
            if self._publish_video_frame(frame, media_epoch):
                return
        owned_frame = _owned_frame(frame)
        if owned_frame is None:
            return
        with self._condition:
            if self._closed:
                return
            self._retained_frame = (QImage(owned_frame), self._media_epoch)
            if not self._enabled:
                return
            self._pending_frame = (owned_frame, self._media_epoch)
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending_frame = None
            self._pending_media_epoch = None
            self._pending_image_transform = None
            self._retained_frame = None
            self._condition.notify_all()
        # Do not call Thread.join() from the Qt close event. A QVideoFrame can
        # finish its Python target while the native multimedia thread is still
        # unwinding. On Windows/Python 3.13 that leaves join() waiting in the OS
        # thread handle past its requested timeout and freezes the GUI teardown.
        # The worker-owned event is signalled only after its publisher cleanup,
        # giving shutdown a real, bounded handshake without joining a native Qt
        # thread from the application event loop.
        if not self._worker_stopped.wait(timeout=_SHUTDOWN_BUDGET_SECONDS):
            log.warning("Content frame ingress did not stop within its shutdown budget")

    def _run(self) -> None:
        next_publish_at = 0.0
        try:
            while True:
                with self._condition:
                    while (
                        not self._closed
                        and self._pending_frame is None
                        and self._pending_media_epoch is None
                        and self._pending_image_transform is None
                    ):
                        self._condition.wait()
                    if self._closed:
                        return
                    remaining = next_publish_at - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    pending = self._pending_frame
                    self._pending_frame = None
                    pending_media_epoch = self._pending_media_epoch
                    self._pending_media_epoch = None
                    pending_image_transform = self._pending_image_transform
                    self._pending_image_transform = None
                publish_started_at = time.monotonic()
                controls_ready = True
                if pending_media_epoch is not None:
                    try:
                        self._publish_media_epoch(pending_media_epoch)
                    except TimeoutError:
                        controls_ready = False
                        with self._condition:
                            if not self._closed and self._pending_media_epoch is None:
                                self._pending_media_epoch = pending_media_epoch
                                self._condition.notify()
                    except FrameChannelStaleFrameError:
                        pass
                    except FrameChannelUnavailableError:
                        log.info("Native content frame transport is unavailable on this platform")
                        self.descriptor_changed.emit(None)
                        return
                    except Exception:  # noqa: BLE001 - native frame transport boundary
                        log.warning("Could not publish the native content epoch", exc_info=True)
                        self._reset_publisher()
                        self.descriptor_changed.emit(None)
                if pending_image_transform is not None:
                    try:
                        self._publish_image_transform(pending_image_transform)
                    except TimeoutError:
                        controls_ready = False
                        with self._condition:
                            if not self._closed and self._pending_image_transform is None:
                                self._pending_image_transform = pending_image_transform
                                self._condition.notify()
                    except FrameChannelUnavailableError:
                        log.info("Native content frame transport is unavailable on this platform")
                        self.descriptor_changed.emit(None)
                        return
                    except Exception:  # noqa: BLE001 - native frame transport boundary
                        log.warning("Could not publish the native image transform", exc_info=True)
                        self._reset_publisher()
                        self.descriptor_changed.emit(None)
                if pending is not None and not controls_ready:
                    # Do not let pixels overtake the epoch-bound transform that
                    # describes them. A newer pending frame remains authoritative.
                    with self._condition:
                        if not self._closed and self._pending_frame is None:
                            self._pending_frame = pending
                            self._condition.notify()
                elif pending is not None:
                    frame, media_epoch = pending
                    try:
                        self._publish(frame, media_epoch)
                    except TimeoutError:
                        # The transport stays non-blocking, but a static image has
                        # no naturally arriving next frame. Retain this attempt
                        # unless a newer frame or epoch already superseded it.
                        with self._condition:
                            if (
                                not self._closed
                                and self._enabled
                                and self._pending_frame is None
                                and media_epoch == self._media_epoch
                            ):
                                self._pending_frame = (frame, media_epoch)
                                self._condition.notify()
                    except FrameChannelStaleFrameError:
                        pass
                    except FrameChannelUnavailableError:
                        log.info("Native content frame transport is unavailable on this platform")
                        self.descriptor_changed.emit(None)
                        return
                    except Exception:  # noqa: BLE001 - native frame transport boundary
                        log.warning("Could not publish a native content frame", exc_info=True)
                        self._reset_publisher()
                        self.descriptor_changed.emit(None)
                # Pace frame *starts*, not frame completions. Scaling and BGRA
                # publication can take several milliseconds at 1080p; adding
                # that work to every interval silently turns 30 fps into a much
                # lower and visibly uneven cadence.
                next_publish_at = publish_started_at + self._minimum_interval
        finally:
            self._reset_publisher()
            self._worker_stopped.set()

    def _publish(self, image: QImage, media_epoch: int) -> None:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            return
        bgra = _prepare_bgra_for_capacity(image, self._canvas_size)
        pixels = _packed_bgra(bgra)
        descriptor: FrameChannelDescriptor | None = None
        with self._publisher_lock:
            publisher = self._publisher
            if publisher is None:
                publisher = self._create_publisher()
                if publisher is None:
                    return
                self._publisher = publisher
                descriptor = publisher.descriptor
            publisher.publish(
                pixels,
                frame_width=bgra.width(),
                frame_height=bgra.height(),
                media_epoch=media_epoch,
            )
        if descriptor is not None:
            self.descriptor_changed.emit(descriptor)

    def _publish_media_epoch(self, media_epoch: int) -> None:
        descriptor: FrameChannelDescriptor | None = None
        created = False
        with self._publisher_lock:
            publisher = self._publisher
            if publisher is None:
                publisher = self._create_publisher()
                if publisher is None:
                    return
                self._publisher = publisher
                descriptor = publisher.descriptor
                created = True
            if not created:
                publisher.set_media_epoch(media_epoch)
        if descriptor is not None:
            self.descriptor_changed.emit(descriptor)

    def _publish_image_transform(self, request: _ImageTransformRequest) -> None:
        descriptor: FrameChannelDescriptor | None = None
        with self._publisher_lock:
            publisher = self._publisher
            if publisher is None:
                publisher = self._create_publisher()
                if publisher is None:
                    return
                self._publisher = publisher
                descriptor = publisher.descriptor
            publisher.set_image_transform(
                request.transform,
                media_epoch=request.media_epoch,
                canvas_width=request.canvas_width,
                canvas_height=request.canvas_height,
                animate=request.animate,
                duration_ms=_TRANSFORM_DURATION_MS,
            )
        if descriptor is not None:
            self.descriptor_changed.emit(descriptor)

    def _publish_video_frame(self, frame: QVideoFrame, media_epoch: int) -> bool:
        if frame.pixelFormat() is not QVideoFrameFormat.PixelFormat.Format_NV12:
            return False
        with self._publisher_lock:
            publisher = self._publisher
            publish_planes = getattr(publisher, "publish_planes", None)
            if publisher is None or not callable(publish_planes):
                return False
            if not frame.map(QVideoFrame.MapMode.ReadOnly):
                return False
            try:
                if frame.planeCount() != 2:
                    return False
                start_us = frame.startTime()
                end_us = frame.endTime()
                timestamp_ns = max(0, start_us) * 1_000
                duration_ns = max(0, end_us - start_us) * 1_000
                try:
                    publish_planes(
                        (frame.bits(0), frame.bits(1)),
                        plane_strides=(frame.bytesPerLine(0), frame.bytesPerLine(1)),
                        frame_width=frame.width(),
                        frame_height=frame.height(),
                        pixel_format=VideoPixelFormat.NV12,
                        presentation_timestamp_ns=timestamp_ns,
                        duration_ns=duration_ns,
                        media_epoch=media_epoch,
                    )
                except TimeoutError:
                    # Latest-frame delivery is intentionally non-blocking. If the
                    # native reader owns the slot, this frame is simply superseded.
                    pass
                except FrameChannelStaleFrameError:
                    pass
                return True
            except (RuntimeError, TypeError, ValueError):
                log.warning("Could not publish a native NV12 content frame", exc_info=True)
                return False
            finally:
                frame.unmap()

    def _create_publisher(self) -> _FramePublisher | None:
        try:
            publisher = self._publisher_factory(
                self._canvas_size.width(),
                self._canvas_size.height(),
            )
            if self._media_epoch != 0:
                publisher.set_media_epoch(self._media_epoch)
            request = self._image_transform
            if request.media_epoch != 0 or request.transform is not None:
                publisher.set_image_transform(
                    request.transform,
                    media_epoch=request.media_epoch,
                    canvas_width=request.canvas_width,
                    canvas_height=request.canvas_height,
                    animate=False,
                    duration_ms=_TRANSFORM_DURATION_MS,
                )
            return publisher
        except FrameChannelUnavailableError:
            log.info("Native content frame transport is unavailable on this platform")
            return None

    def _reset_publisher(self) -> None:
        with self._publisher_lock:
            publisher = self._publisher
            self._publisher = None
        if publisher is None:
            return
        try:
            publisher.close()
        except Exception:  # noqa: BLE001 - native resource cleanup boundary
            log.warning("Could not close native content frame channel", exc_info=True)


def _owned_frame(frame: object) -> QImage | None:
    if isinstance(frame, QImage):
        return QImage(frame)
    if isinstance(frame, QVideoFrame) and frame.isValid():
        # QVideoFrame can retain a Media Foundation/D3D surface owned by the
        # QMediaPlayer. Converting it on the Python worker races Qt multimedia
        # teardown and can strand that worker in native code during close.
        # Materialize the implicitly-shared QImage while the delivery callback
        # and its producer are still alive; the worker only receives an owned,
        # thread-safe image afterwards.
        image = frame.toImage()
        return None if image.isNull() else QImage(image)
    to_image = getattr(frame, "toImage", None)
    if callable(to_image):
        image: Any = to_image()
        if isinstance(image, QImage) and not image.isNull():
            return QImage(image)
    return None


def _packed_bgra(image: QImage) -> memoryview | bytearray:
    width_bytes = image.width() * 4
    source = image.constBits()
    if image.bytesPerLine() == width_bytes:
        return memoryview(source)[: width_bytes * image.height()]
    packed = bytearray(width_bytes * image.height())
    for row in range(image.height()):
        source_start = row * image.bytesPerLine()
        target_start = row * width_bytes
        packed[target_start : target_start + width_bytes] = source[
            source_start : source_start + width_bytes
        ]
    return packed


def _prepare_bgra_for_capacity(image: QImage, capacity: QSize) -> QImage:
    bgra = image.convertToFormat(QImage.Format.Format_ARGB32)
    if bgra.width() <= capacity.width() and bgra.height() <= capacity.height():
        return bgra
    # Only oversized inputs are reduced on the CPU. Normal 720p/1080p video is
    # published at source resolution and scaled/letterboxed by d3d11convert in
    # the compositor, avoiding a full 8 MiB smooth scale and canvas clear on
    # every frame.
    return bgra.scaled(
        capacity,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _advance_video_deadline(
    current_deadline: float,
    now: float,
    interval: float,
) -> float | None:
    # Delivery clocks and 29.97/59.94 sources naturally arrive a fraction of a
    # millisecond around a nominal 30/60 fps boundary. A small bounded tolerance
    # avoids turning harmless early jitter into a whole dropped output frame.
    tolerance = min(0.002, interval * 0.1)
    if current_deadline > 0.0 and now + tolerance < current_deadline:
        return None
    next_deadline = (
        current_deadline + interval if current_deadline > 0.0 else now + interval
    )
    if next_deadline <= now:
        missed_intervals = int((now - next_deadline) // interval) + 1
        next_deadline += missed_intervals * interval
    return next_deadline
