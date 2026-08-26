from __future__ import annotations

import logging
import threading
import time
from collections.abc import Buffer, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QVideoSink

from solin.core.foundation.constants import NATIVE_QT_MEDIA_BRIDGE_ENABLED
from solin.core.scenes.engine import FrameChannelDescriptor
from solin.core.scenes.frame_channel import (
    FrameChannelStaleFrameError,
    FrameChannelUnavailableError,
    SharedMemoryVideoFramePublisher,
)
from solin.core.scenes.model import VideoPixelFormat
from solin.core.scenes.qt_media_bridge import (
    QtMediaBridgeSubmitResult,
    create_qt_media_bridge_publisher,
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
_ACCELERATED_WARMUP_FRAMES = 3
_ACCELERATED_TRANSPORT_FAILURES = frozenset(
    {
        "content_d3d11_device_lost",
        "content_d3d11_ingress_failed",
    }
)


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


class _AcceleratedFramePublisher(Protocol):
    @property
    def descriptor(self) -> FrameChannelDescriptor: ...

    def begin_session(self, session_id: int, *, enabled: bool) -> None: ...

    def set_enabled(self, enabled: bool) -> None: ...

    def set_decoder_frame_gate(
        self,
        playback_session_id: int,
        *,
        accepting_frames: bool,
    ) -> None: ...

    def set_direct_submission(self, enabled: bool, *, maximum_fps: int) -> None: ...

    def status(self) -> dict[str, object]: ...

    def stage_media_epoch(self, media_epoch: int) -> None: ...

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

    def submit(
        self,
        frame: QVideoFrame,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult: ...

    def submit_image(
        self,
        image: QImage,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult: ...

    def bind_video_sink(self, sink: QVideoSink) -> None: ...

    def unbind_video_sink(self) -> None: ...

    def close(self) -> None: ...


class ContentFrameIngressController(QObject):
    """Coalesce Qt content frames into the native engine's bounded data plane."""

    descriptor_changed = Signal(object)
    direct_submission_changed = Signal(bool)
    _close_accelerated_requested = Signal(object)
    _descriptor_change_requested = Signal(object)
    _direct_submission_state_requested = Signal(bool)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        publisher_factory: Callable[[int, int], _FramePublisher] | None = None,
        accelerated_publisher_factory: (
            Callable[[int, int], _AcceleratedFramePublisher | None] | None
        ) = None,
        enable_accelerated: bool | None = None,
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
        self._accelerated_publisher_factory = (
            accelerated_publisher_factory or create_qt_media_bridge_publisher
        )
        self._accelerated_enabled = (
            publisher_factory is None and NATIVE_QT_MEDIA_BRIDGE_ENABLED
            if enable_accelerated is None
            else bool(enable_accelerated)
        )
        self._canvas_size = QSize(canvas_width, canvas_height)
        self._maximum_fps = maximum_fps
        self._minimum_interval = 1.0 / maximum_fps
        self._condition = threading.Condition()
        self._publisher_lock = threading.Lock()
        self._worker_stopped = threading.Event()
        self._close_accelerated_requested.connect(
            self._close_accelerated_on_owner,
            Qt.ConnectionType.QueuedConnection,
        )
        self._descriptor_change_requested.connect(
            self._deliver_descriptor_change,
            Qt.ConnectionType.QueuedConnection,
        )
        self._direct_submission_state_requested.connect(
            self._set_direct_submission_active,
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
        self._decoder_frame_gate = (0, False)
        self._direct_submission_active = False
        self._direct_activation_epoch: int | None = None
        self._accelerated_unavailable_frames = 0
        self._accelerated_publisher = self._create_accelerated_publisher()
        self._retired_accelerated_publishers: list[_AcceleratedFramePublisher] = []
        self._active_accelerated = self._accelerated_publisher is not None
        self._publisher: _FramePublisher | None = (
            None if self._active_accelerated else self._create_publisher()
        )
        log.info(
            "Native content ingress transport selected: %s",
            (
                "d3d11_shared_texture"
                if self._active_accelerated
                else "shared_memory"
                if self._publisher is not None
                else "unavailable"
            ),
        )
        self._worker = threading.Thread(
            target=self._run,
            name="solin-content-frame-ingress",
            daemon=True,
        )
        self._worker.start()
        self._direct_health_timer = QTimer(self)
        self._direct_health_timer.setInterval(500)
        self._direct_health_timer.timeout.connect(self._check_direct_submission_health)

    @property
    def descriptor(self) -> FrameChannelDescriptor | None:
        with self._publisher_lock:
            if self._active_accelerated and self._accelerated_publisher is not None:
                return self._accelerated_publisher.descriptor
            publisher = self._publisher
            return publisher.descriptor if publisher is not None else None

    @property
    def enabled(self) -> bool:
        with self._condition:
            return self._enabled and not self._closed

    @property
    def direct_submission_active(self) -> bool:
        return self._direct_submission_active

    @Slot(int, bool)
    def set_decoder_frame_gate(
        self,
        playback_session_id: int,
        accepting_frames: bool,
    ) -> None:
        if (
            isinstance(playback_session_id, bool)
            or not isinstance(playback_session_id, int)
            or not 0 <= playback_session_id <= 2**64 - 1
        ):
            raise ValueError("Content playback session must be a non-negative 64-bit integer")
        state = (playback_session_id, bool(accepting_frames))
        with self._condition:
            if self._closed or state[0] < self._decoder_frame_gate[0]:
                return
            self._decoder_frame_gate = state
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active = self._active_accelerated
        if accelerated is None or not active:
            return
        try:
            accelerated.set_decoder_frame_gate(
                playback_session_id,
                accepting_frames=state[1],
            )
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning(
                "Could not update accelerated decoded-frame acceptance",
                exc_info=True,
            )
            self._disable_accelerated_publisher(expected=accelerated)
            self._activate_fallback()

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
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active_accelerated = self._active_accelerated
        if accelerated is not None and active_accelerated:
            try:
                accelerated.set_enabled(enabled)
            except Exception:  # noqa: BLE001 - optional native ABI boundary
                log.warning(
                    "Could not update accelerated Qt media ingress demand",
                    exc_info=True,
                )
                self._disable_accelerated_publisher(expected=accelerated)
                self._activate_fallback()

    def bind_video_sink(self, sink: QVideoSink) -> bool:
        if not isinstance(sink, QVideoSink):
            raise TypeError("Content ingress video sink must be a QVideoSink")
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
        if accelerated is None:
            return False
        try:
            accelerated.bind_video_sink(sink)
            return True
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning(
                "Could not bind the decoder sink to accelerated Qt media ingress",
                exc_info=True,
            )
            self._disable_accelerated_publisher(expected=accelerated)
            self._activate_fallback()
            return False

    def recover_accelerated_transport(self, error_code: str) -> bool:
        """Permanently select compatibility ingress after a sidecar GPU failure."""
        if error_code not in _ACCELERATED_TRANSPORT_FAILURES:
            return False
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active = self._active_accelerated
        if accelerated is None or not active:
            return False
        log.warning(
            "Accelerated Qt media ingress is incompatible with the native renderer (%s); "
            "selecting shared-memory compatibility transport",
            error_code,
        )
        # Adapter mismatch and device loss are not frame-local. Retiring this
        # bridge prevents every following hardware frame from bouncing the
        # descriptor back to a route the sidecar has already rejected.
        self._disable_accelerated_publisher(expected=accelerated)
        if self._activate_fallback():
            return True
        self.descriptor_changed.emit(None)
        return False

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
            if (
                self._retained_frame is not None
                and self._retained_frame[1] < media_epoch
            ):
                self._retained_frame = None
            if (
                self._pending_image_transform is not None
                and self._pending_image_transform.media_epoch < media_epoch
            ):
                self._pending_image_transform = None
            route_enabled = self._enabled
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active_accelerated = self._active_accelerated
        if accelerated is not None and active_accelerated:
            try:
                # A bound QVideoSink has no presentation identity of its own.
                # Stop direct callbacks at every identity boundary so a late
                # decoder frame cannot be relabelled as the new presentation.
                self._direct_activation_epoch = None
                accelerated.set_direct_submission(
                    False,
                    maximum_fps=self._maximum_fps,
                )
                accelerated.begin_session(
                    media_epoch,
                    enabled=route_enabled,
                )
                # D3D11 stages this value locally. The channel publishes it
                # atomically only after the corresponding texture copy has
                # completed, matching SharedMemoryVideoFramePublisher.publish.
                accelerated.stage_media_epoch(media_epoch)
                self._request_direct_submission_state(False)
            except Exception:  # noqa: BLE001 - optional native ABI boundary
                log.warning(
                    "Could not advance the accelerated Qt media ingress session",
                    exc_info=True,
                )
                self._disable_accelerated_publisher(expected=accelerated)
                self._activate_fallback()

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
        if isinstance(frame, QVideoFrame):
            if not frame.isValid():
                return
            if self._direct_submission_active:
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
            if self._publish_accelerated_video_frame(frame, media_epoch):
                return
            if self._publish_video_frame_fallback(frame, media_epoch):
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
            self._pending_image_transform = None
            self._retained_frame = None
            self._condition.notify_all()
        self._request_direct_submission_state(False)
        self._disable_accelerated_publisher()
        self._close_all_retired_accelerated_publishers()
        # Do not call Thread.join() from the Qt close event. A QVideoFrame can
        # finish its Python target while the native multimedia thread is still
        # unwinding. On Windows/Python 3.13 that leaves join() waiting in the OS
        # thread handle past its requested timeout and freezes the GUI teardown.
        # The worker-owned event is signalled only after its publisher cleanup,
        # giving shutdown a real, bounded handshake without joining a native Qt
        # thread from the application event loop.
        if not self._worker_stopped.wait(timeout=_SHUTDOWN_BUDGET_SECONDS):
            log.warning("Content frame ingress did not stop within its shutdown budget")

    def _publish_accelerated_video_frame(
        self,
        frame: QVideoFrame,
        media_epoch: int,
    ) -> bool:
        accelerated_compatible = (
            frame.pixelFormat() is QVideoFrameFormat.PixelFormat.Format_NV12
            and frame.handleType() is QVideoFrame.HandleType.RhiTextureHandle
        )
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active = self._active_accelerated
        if accelerated is None:
            return False
        if not accelerated_compatible:
            if not active:
                return False
            self._accelerated_unavailable_frames += 1
            if self._accelerated_unavailable_frames < _ACCELERATED_WARMUP_FRAMES:
                return True
            return False
        with self._condition:
            if (
                self._closed
                or not self._enabled
                or media_epoch != self._media_epoch
                or not self._decoder_frame_gate[1]
            ):
                return True
            route_enabled = self._enabled
            decoder_frame_gate = self._decoder_frame_gate
            request = self._image_transform
        descriptor: FrameChannelDescriptor | None = None
        route_active = active
        try:
            with self._publisher_lock:
                accelerated = self._accelerated_publisher
                if accelerated is None:
                    return False
                route_active = self._active_accelerated
                if not route_active:
                    accelerated.begin_session(
                        media_epoch,
                        enabled=route_enabled,
                    )
                    accelerated.set_decoder_frame_gate(
                        decoder_frame_gate[0],
                        accepting_frames=decoder_frame_gate[1],
                    )
                    accelerated.set_direct_submission(
                        False,
                        maximum_fps=self._maximum_fps,
                    )
                    accelerated.stage_media_epoch(media_epoch)
                    accelerated.set_image_transform(
                        request.transform,
                        media_epoch=request.media_epoch,
                        canvas_width=request.canvas_width,
                        canvas_height=request.canvas_height,
                        animate=False,
                        duration_ms=_TRANSFORM_DURATION_MS,
                    )
                result = accelerated.submit(frame, session_id=media_epoch)
                if not route_active and result is QtMediaBridgeSubmitResult.ACCEPTED:
                    with self._condition:
                        still_current = (
                            not self._closed
                            and self._enabled
                            and media_epoch == self._media_epoch
                            and self._decoder_frame_gate == decoder_frame_gate
                            and decoder_frame_gate[1]
                        )
                    if still_current:
                        self._active_accelerated = True
                        route_active = True
                        descriptor = accelerated.descriptor
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning("Accelerated Qt media ingress failed", exc_info=True)
            self._disable_accelerated_publisher()
            self._activate_fallback()
            return False
        if result in {
            QtMediaBridgeSubmitResult.ACCEPTED,
            QtMediaBridgeSubmitResult.DROPPED,
            QtMediaBridgeSubmitResult.NO_DEMAND,
            QtMediaBridgeSubmitResult.SESSION_REJECTED,
        }:
            self._accelerated_unavailable_frames = 0
            if descriptor is not None:
                log.info("Native content ingress transport selected: d3d11_shared_texture")
                self._request_descriptor_change(descriptor)
            if result is QtMediaBridgeSubmitResult.ACCEPTED and route_active:
                self._schedule_direct_submission_activation(
                    accelerated,
                    media_epoch,
                )
            return True
        self._accelerated_unavailable_frames += 1
        if self._accelerated_unavailable_frames < _ACCELERATED_WARMUP_FRAMES:
            return True
        return False

    def _schedule_direct_submission_activation(
        self,
        accelerated: _AcceleratedFramePublisher,
        media_epoch: int,
    ) -> None:
        if (
            self._direct_submission_active
            or self._direct_activation_epoch == media_epoch
        ):
            return
        self._direct_activation_epoch = media_epoch
        QTimer.singleShot(
            0,
            lambda: self._activate_direct_submission(accelerated, media_epoch),
        )

    def _activate_direct_submission(
        self,
        accelerated: _AcceleratedFramePublisher,
        media_epoch: int,
    ) -> None:
        if self._direct_activation_epoch != media_epoch:
            return
        self._direct_activation_epoch = None
        with self._condition:
            eligible = (
                not self._closed
                and self._enabled
                and self._media_epoch == media_epoch
                and self._decoder_frame_gate[1]
            )
        with self._publisher_lock:
            eligible = (
                eligible and self._active_accelerated and self._accelerated_publisher is accelerated
            )
        if not eligible:
            return
        try:
            accelerated.set_direct_submission(
                True,
                maximum_fps=self._maximum_fps,
            )
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning(
                "Could not activate direct Qt media submission",
                exc_info=True,
            )
            self._disable_accelerated_publisher(expected=accelerated)
            self._activate_fallback()
            return
        self._set_direct_submission_active(True)

    def _request_direct_submission_state(self, active: bool) -> None:
        if QThread.currentThread() == self.thread():
            self._set_direct_submission_active(active)
        else:
            self._direct_submission_state_requested.emit(bool(active))

    @Slot(bool)
    def _set_direct_submission_active(self, active: bool) -> None:
        active = bool(active)
        if self._direct_submission_active == active:
            return
        self._direct_submission_active = active
        if active:
            self._direct_health_timer.start()
        else:
            self._direct_health_timer.stop()
        self.direct_submission_changed.emit(active)

    @Slot()
    def _check_direct_submission_health(self) -> None:
        if not self._direct_submission_active:
            return
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            active = self._active_accelerated
        if accelerated is None or not active:
            self._set_direct_submission_active(False)
            return
        try:
            status = accelerated.status()
            available = bool(status.get("available", False))
            error_code = str(status.get("error_code", "") or "")
            native_direct_active = bool(status.get("direct_submission_active", False))
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            available = False
            error_code = "d3d11_bridge_status_unavailable"
            native_direct_active = False
        if available and not error_code and native_direct_active:
            return
        if available and not error_code:
            self._set_direct_submission_active(False)
            return
        log.warning(
            "Direct Qt media submission failed (%s); selecting compatibility transport",
            error_code or "d3d11_bridge_unavailable",
        )
        self._disable_accelerated_publisher(expected=accelerated)
        self._activate_fallback()

    def _activate_fallback(self) -> bool:
        descriptor: FrameChannelDescriptor | None = None
        accelerated: _AcceleratedFramePublisher | None = None
        failed_publisher: _FramePublisher | None = None
        try:
            with self._publisher_lock:
                if not self._active_accelerated and self._publisher is not None:
                    return True
                publisher = self._publisher
                if publisher is None:
                    publisher = self._create_publisher()
                    if publisher is None:
                        return False
                    self._publisher = publisher
                self._synchronize_fallback_state(
                    publisher,
                    media_epoch=self._media_epoch,
                    request=self._image_transform,
                )
                accelerated = self._accelerated_publisher
                self._active_accelerated = False
                descriptor = publisher.descriptor
        except Exception:  # noqa: BLE001 - native frame transport boundary
            log.warning("Could not activate fallback content ingress", exc_info=True)
            with self._publisher_lock:
                failed_publisher = self._publisher
                self._publisher = None
            if failed_publisher is not None:
                try:
                    failed_publisher.close()
                except Exception:  # noqa: BLE001 - native resource cleanup boundary
                    log.warning(
                        "Could not close failed fallback content ingress",
                        exc_info=True,
                    )
            return False
        if accelerated is not None:
            self._suspend_accelerated_for_fallback(accelerated)
        else:
            self._request_direct_submission_state(False)
        self._accelerated_unavailable_frames = 0
        log.info("Native content ingress transport selected: shared_memory")
        self._request_descriptor_change(descriptor)
        return True

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
            if self._active_accelerated and self._accelerated_publisher is not None:
                current = self._accelerated_publisher.descriptor
            else:
                publisher = self._publisher
                current = publisher.descriptor if publisher is not None else None
        if typed != current:
            return
        self.descriptor_changed.emit(typed)

    def _disable_accelerated_publisher(
        self,
        *,
        expected: _AcceleratedFramePublisher | None = None,
    ) -> None:
        self._direct_activation_epoch = None
        self._request_direct_submission_state(False)
        with self._publisher_lock:
            accelerated = self._accelerated_publisher
            if expected is not None and accelerated is not expected:
                return
            self._accelerated_publisher = None
            self._active_accelerated = False
            if accelerated is not None:
                self._retired_accelerated_publishers.append(accelerated)
        if accelerated is None:
            return
        if QThread.currentThread() == self.thread():
            self._close_accelerated_on_owner(accelerated)
        else:
            self._close_accelerated_requested.emit(accelerated)

    @Slot(object)
    def _close_accelerated_on_owner(self, publisher: object) -> None:
        typed = cast(_AcceleratedFramePublisher, publisher)
        try:
            typed.unbind_video_sink()
            typed.close()
        except Exception:  # noqa: BLE001 - native resource cleanup boundary
            log.warning("Could not close accelerated Qt media ingress", exc_info=True)
        finally:
            with self._publisher_lock:
                try:
                    self._retired_accelerated_publishers.remove(typed)
                except ValueError:
                    pass

    def _close_all_retired_accelerated_publishers(self) -> None:
        with self._publisher_lock:
            retired = tuple(self._retired_accelerated_publishers)
        for publisher in retired:
            self._close_accelerated_on_owner(publisher)

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
                        with self._publisher_lock:
                            accelerated_still_active = (
                                self._active_accelerated and self._accelerated_publisher is not None
                            )
                        if not accelerated_still_active:
                            self.descriptor_changed.emit(None)
                # Pace frame *starts*, not frame completions. Scaling and BGRA
                # publication can take several milliseconds at 1080p; adding
                # that work to every interval silently turns 30 fps into a much
                # lower and visibly uneven cadence.
                next_publish_at = publish_started_at + self._minimum_interval
        finally:
            self._reset_publisher()
            self._disable_accelerated_publisher()
            self._worker_stopped.set()

    def _publish(self, image: QImage, media_epoch: int) -> None:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            return
        bgra = _prepare_bgra_for_capacity(image, self._canvas_size)
        with self._condition:
            if self._closed or not self._enabled or media_epoch != self._media_epoch:
                raise FrameChannelStaleFrameError
            request = self._image_transform
        if self._publish_accelerated_image(bgra, media_epoch, request):
            return
        pixels = _packed_bgra(bgra)
        descriptor: FrameChannelDescriptor | None = None
        accelerated: _AcceleratedFramePublisher | None = None
        with self._publisher_lock:
            publisher = self._publisher
            created = publisher is None
            if publisher is None:
                publisher = self._create_publisher()
                if publisher is None:
                    return
                self._publisher = publisher
            activating_fallback = self._active_accelerated
            if created or activating_fallback:
                self._synchronize_fallback_state(
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
            if activating_fallback:
                accelerated = self._accelerated_publisher
                self._active_accelerated = False
                descriptor = publisher.descriptor
            elif created:
                descriptor = publisher.descriptor
        if accelerated is not None:
            self._suspend_accelerated_for_fallback(accelerated)
        if descriptor is not None:
            if accelerated is not None:
                self._accelerated_unavailable_frames = 0
                log.info("Native content ingress transport selected: shared_memory")
            self._request_descriptor_change(descriptor)

    def _publish_accelerated_image(
        self,
        image: QImage,
        media_epoch: int,
        request: _ImageTransformRequest,
    ) -> bool:
        descriptor: FrameChannelDescriptor | None = None
        accelerated: _AcceleratedFramePublisher | None = None
        try:
            with self._publisher_lock:
                accelerated = self._accelerated_publisher
                if accelerated is None:
                    return False
                route_active = self._active_accelerated
                if not route_active:
                    accelerated.begin_session(media_epoch, enabled=self._enabled)
                    accelerated.set_direct_submission(
                        False,
                        maximum_fps=self._maximum_fps,
                    )
                    accelerated.stage_media_epoch(media_epoch)
                    if request.media_epoch == media_epoch:
                        accelerated.set_image_transform(
                            request.transform,
                            media_epoch=request.media_epoch,
                            canvas_width=request.canvas_width,
                            canvas_height=request.canvas_height,
                            animate=False,
                            duration_ms=_TRANSFORM_DURATION_MS,
                        )
                result = accelerated.submit_image(image, session_id=media_epoch)
                if not route_active and result is QtMediaBridgeSubmitResult.ACCEPTED:
                    with self._condition:
                        still_current = (
                            not self._closed
                            and self._enabled
                            and media_epoch == self._media_epoch
                        )
                    if still_current:
                        self._active_accelerated = True
                        descriptor = accelerated.descriptor
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning("Accelerated static media ingress failed", exc_info=True)
            self._disable_accelerated_publisher(expected=accelerated)
            return False
        if result in {
            QtMediaBridgeSubmitResult.DROPPED,
            QtMediaBridgeSubmitResult.SESSION_REJECTED,
        }:
            # Unlike video, a still image has no following decoder callback to
            # replace a busy or concurrently superseded submission. Preserve
            # the current epoch in the bounded worker and retry it instead of
            # silently leaving the previous texture visible.
            raise TimeoutError("Accelerated static frame was not committed")
        if result in {
            QtMediaBridgeSubmitResult.ACCEPTED,
            QtMediaBridgeSubmitResult.NO_DEMAND,
        }:
            if descriptor is not None:
                log.info("Native content ingress transport selected: d3d11_shared_texture")
                self._request_descriptor_change(descriptor)
            return True
        return False

    @staticmethod
    def _synchronize_fallback_state(
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

    def _suspend_accelerated_for_fallback(
        self,
        accelerated: _AcceleratedFramePublisher,
    ) -> None:
        try:
            accelerated.set_direct_submission(
                False,
                maximum_fps=self._maximum_fps,
            )
            accelerated.set_enabled(False)
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning(
                "Could not suspend accelerated Qt media ingress",
                exc_info=True,
            )
            self._disable_accelerated_publisher(expected=accelerated)
        self._request_direct_submission_state(False)

    def _publish_image_transform(self, request: _ImageTransformRequest) -> None:
        descriptor: FrameChannelDescriptor | None = None
        for _attempt in range(2):
            accelerated_error: Exception | None = None
            accelerated: _AcceleratedFramePublisher | None = None
            with self._publisher_lock:
                accelerated = self._accelerated_publisher
                if self._active_accelerated and accelerated is not None:
                    try:
                        accelerated.set_image_transform(
                            request.transform,
                            media_epoch=request.media_epoch,
                            canvas_width=request.canvas_width,
                            canvas_height=request.canvas_height,
                            animate=request.animate,
                            duration_ms=_TRANSFORM_DURATION_MS,
                        )
                        return
                    except Exception as error:  # noqa: BLE001 - optional native ABI boundary
                        accelerated_error = error
                else:
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
                    break
            if accelerated_error is not None:
                log.warning(
                    "Could not publish the accelerated image transform",
                    exc_info=(
                        type(accelerated_error),
                        accelerated_error,
                        accelerated_error.__traceback__,
                    ),
                )
                self._disable_accelerated_publisher(expected=accelerated)
                if not self._activate_fallback():
                    raise accelerated_error
        if descriptor is not None:
            self._request_descriptor_change(descriptor)

    def _publish_video_frame_fallback(
        self,
        frame: QVideoFrame,
        media_epoch: int,
    ) -> bool:
        if frame.pixelFormat() is not QVideoFrameFormat.PixelFormat.Format_NV12:
            return False
        with self._publisher_lock:
            # A standby SHM channel is not the active Program source yet. Its
            # first frame must go through the transactional QImage handoff so
            # the descriptor cannot remain on an incompatible GPU route.
            if self._active_accelerated:
                return False
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
            return self._publisher_factory(
                self._canvas_size.width(),
                self._canvas_size.height(),
            )
        except FrameChannelUnavailableError:
            log.info("Native content frame transport is unavailable on this platform")
            return None

    def _create_accelerated_publisher(self) -> _AcceleratedFramePublisher | None:
        if not self._accelerated_enabled:
            return None
        try:
            return self._accelerated_publisher_factory(
                self._canvas_size.width(),
                self._canvas_size.height(),
            )
        except Exception:  # noqa: BLE001 - optional native ABI boundary
            log.warning("Could not initialize accelerated Qt media ingress", exc_info=True)
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
    next_deadline = current_deadline + interval if current_deadline > 0.0 else now + interval
    if next_deadline <= now:
        missed_intervals = int((now - next_deadline) // interval) + 1
        next_deadline += missed_intervals * interval
    return next_deadline
