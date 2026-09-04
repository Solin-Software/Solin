from __future__ import annotations

import logging
import threading
import time
from collections.abc import Buffer, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from solin.core.scenes.engine import FrameChannelDescriptor
from solin.core.scenes.frame_channel import (
    FrameChannelStaleFrameError,
    FrameChannelUnavailableError,
    SharedMemoryVideoFramePublisher,
)
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
    """Coalesce app-rendered content frames into the sidecar's data plane.

    libobs is the only media engine, so media video is decoded and composited by
    the sidecar — never pushed here. This controller carries only the app's own
    rendered content (yeartext, images, timers) as BGRA frames over a
    cross-platform shared-memory channel.
    """

    descriptor_changed = Signal(object)
    _descriptor_change_requested = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        publisher_factory: Callable[[int, int], _FramePublisher] | None = None,
        accelerated_publisher_factory: Callable[..., object] | None = None,
        enable_accelerated: bool | None = None,
        maximum_fps: int = _DEFAULT_MAXIMUM_FPS,
        canvas_width: int = _DEFAULT_CANVAS_WIDTH,
        canvas_height: int = _DEFAULT_CANVAS_HEIGHT,
    ) -> None:
        super().__init__(parent)
        # accelerated_publisher_factory / enable_accelerated are accepted for
        # backward-compatible call sites but ignored — the Qt/D3D11 accelerated
        # path is gone with QtMultimedia.
        del accelerated_publisher_factory, enable_accelerated
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
        self._maximum_fps = maximum_fps
        self._minimum_interval = 1.0 / maximum_fps
        self._condition = threading.Condition()
        self._publisher_lock = threading.Lock()
        self._worker_stopped = threading.Event()
        self._descriptor_change_requested.connect(
            self._deliver_descriptor_change,
            Qt.ConnectionType.QueuedConnection,
        )
        self._pending_frame: tuple[QImage, int] | None = None
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
        log.info(
            "Content ingress transport selected: %s",
            "shared_memory" if self._publisher is not None else "unavailable",
        )
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

    def recover_accelerated_transport(self, error_code: str) -> bool:
        # No accelerated transport exists any more; the shared-memory channel is
        # already the only route. Kept for call-site compatibility.
        del error_code
        return False

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

    def begin_presentation(self, media_epoch: int) -> None:
        """Arm one presentation without exposing it before its first frame."""
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
            self._next_video_materialization_at = 0.0
            if self._pending_frame is not None and self._pending_frame[1] < media_epoch:
                self._pending_frame = None
            if self._retained_frame is not None and self._retained_frame[1] < media_epoch:
                self._retained_frame = None
            if (
                self._pending_image_transform is not None
                and self._pending_image_transform.media_epoch < media_epoch
            ):
                self._pending_image_transform = None

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
            transform=(normalize_image_transform(transform) if transform is not None else None),
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
            self._pending_image_transform = None
            self._retained_frame = None
            self._condition.notify_all()
        if not self._worker_stopped.wait(timeout=_SHUTDOWN_BUDGET_SECONDS):
            log.warning("Content frame ingress did not stop within its shutdown budget")

    # ── worker ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        next_publish_at = 0.0
        try:
            while True:
                with self._condition:
                    while (
                        not self._closed
                        and self._pending_frame is None
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
                    pending_image_transform = self._pending_image_transform
                    self._pending_image_transform = None
                publish_started_at = time.monotonic()
                controls_ready = True
                if pending_image_transform is not None:
                    try:
                        self._publish_image_transform(pending_image_transform)
                    except FrameChannelUnavailableError:
                        log.info("Content frame transport is unavailable on this platform")
                        self.descriptor_changed.emit(None)
                        return
                    except Exception:  # noqa: BLE001 - native frame transport boundary
                        log.warning("Could not publish the content image transform", exc_info=True)
                        self._reset_publisher()
                        self.descriptor_changed.emit(None)
                if pending is not None and not controls_ready:
                    with self._condition:
                        if not self._closed and self._pending_frame is None:
                            self._pending_frame = pending
                            self._condition.notify()
                elif pending is not None:
                    frame, media_epoch = pending
                    try:
                        self._publish(frame, media_epoch)
                    except TimeoutError:
                        # A static image has no naturally arriving next frame;
                        # retain this attempt unless something superseded it.
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
                        log.info("Content frame transport is unavailable on this platform")
                        self.descriptor_changed.emit(None)
                        return
                    except Exception:  # noqa: BLE001 - native frame transport boundary
                        log.warning("Could not publish a content frame", exc_info=True)
                        self._reset_publisher()
                        self.descriptor_changed.emit(None)
                next_publish_at = publish_started_at + self._minimum_interval
        finally:
            self._reset_publisher()
            self._worker_stopped.set()

    def _publish(self, image: QImage, media_epoch: int) -> None:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            return
        bgra = _prepare_bgra_for_capacity(image, self._canvas_size)
        with self._condition:
            if self._closed or not self._enabled or media_epoch != self._media_epoch:
                raise FrameChannelStaleFrameError
            request = self._image_transform
        pixels = _packed_bgra(bgra)
        descriptor: FrameChannelDescriptor | None = None
        with self._publisher_lock:
            publisher = self._publisher
            created = publisher is None
            if publisher is None:
                publisher = self._create_publisher()
                if publisher is None:
                    return
                self._publisher = publisher
            if created:
                self._synchronize_publisher_state(
                    publisher,
                    media_epoch=media_epoch,
                    request=request,
                )
            publisher.publish(
                pixels,
                frame_width=bgra.width(),
                frame_height=bgra.height(),
                media_epoch=media_epoch,
            )
            with self._condition:
                if self._closed or not self._enabled or media_epoch != self._media_epoch:
                    raise FrameChannelStaleFrameError
            if created:
                descriptor = publisher.descriptor
        if descriptor is not None:
            self._request_descriptor_change(descriptor)

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
            self._request_descriptor_change(descriptor)

    @staticmethod
    def _synchronize_publisher_state(
        publisher: _FramePublisher,
        *,
        media_epoch: int,
        request: _ImageTransformRequest,
    ) -> None:
        if request.media_epoch == media_epoch and (
            request.media_epoch != 0 or request.transform is not None
        ):
            publisher.set_image_transform(
                request.transform,
                media_epoch=request.media_epoch,
                canvas_width=request.canvas_width,
                canvas_height=request.canvas_height,
                animate=False,
                duration_ms=_TRANSFORM_DURATION_MS,
            )

    def _request_descriptor_change(
        self,
        descriptor: FrameChannelDescriptor | None,
    ) -> None:
        if QThread.currentThread() == self.thread():
            self._deliver_descriptor_change(descriptor)
        else:
            self._descriptor_change_requested.emit(descriptor)

    @Slot(object)
    def _deliver_descriptor_change(self, descriptor: object) -> None:
        typed = cast(FrameChannelDescriptor | None, descriptor)
        with self._publisher_lock:
            publisher = self._publisher
            current = publisher.descriptor if publisher is not None else None
        if typed != current:
            return
        self.descriptor_changed.emit(typed)

    def _create_publisher(self) -> _FramePublisher | None:
        try:
            return self._publisher_factory(
                self._canvas_size.width(),
                self._canvas_size.height(),
            )
        except FrameChannelUnavailableError:
            log.info("Content frame transport is unavailable on this platform")
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
            log.warning("Could not close content frame channel", exc_info=True)


def _owned_frame(frame: object) -> QImage | None:
    if isinstance(frame, QImage):
        return QImage(frame)
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
    return bgra.scaled(
        capacity,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
