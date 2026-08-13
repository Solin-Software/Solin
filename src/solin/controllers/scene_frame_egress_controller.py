from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from solin.core.scenes.engine import FrameChannelDescriptor
from solin.core.scenes.frame_channel import (
    FrameChannelUnavailableError,
    SharedMemoryBgraFrameSubscriber,
    SharedMemoryVideoFrameSubscriber,
    VideoFrame,
)
from solin.core.scenes.model import VideoPixelFormat


log = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 1.0 / 60.0


class _FrameSubscriber(Protocol):
    @property
    def descriptor(self) -> FrameChannelDescriptor: ...

    def read_latest(self) -> VideoFrame | None: ...

    def close(self) -> None: ...


class _SceneFrameEgressController(QObject):
    """Consume a native media bus without sending video through JSON IPC."""

    frame_ready = Signal(object)
    descriptor_changed = Signal(object)

    def __init__(
        self,
        width: int,
        height: int,
        parent: QObject | None = None,
        *,
        worker_name: str = "solin-scene-frame-egress",
    ) -> None:
        super().__init__(parent)
        self._condition = threading.Condition()
        self._subscriber: _FrameSubscriber | None = None
        self._closed = False
        self._configure_locked(width, height)
        self._worker = threading.Thread(
            target=self._run,
            name=worker_name,
            daemon=True,
        )
        self._worker.start()

    @property
    def descriptor(self) -> FrameChannelDescriptor | None:
        with self._condition:
            subscriber = self._subscriber
            return None if subscriber is None else subscriber.descriptor

    def reconfigure(self, width: int, height: int) -> None:
        replacement: _FrameSubscriber | None
        try:
            replacement = self._create_subscriber(width, height)
        except FrameChannelUnavailableError:
            replacement = None
        with self._condition:
            if self._closed:
                if replacement is not None:
                    replacement.close()
                return
            current = self._subscriber
            if (
                current is not None
                and replacement is not None
                and current.descriptor.width == replacement.descriptor.width
                and current.descriptor.height == replacement.descriptor.height
            ):
                replacement.close()
                return
            self._subscriber = replacement
            self._condition.notify_all()
        if current is not None:
            current.close()
        self.descriptor_changed.emit(
            None if replacement is None else replacement.descriptor
        )

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            subscriber = self._subscriber
            self._subscriber = None
            self._condition.notify_all()
        self._worker.join(timeout=2.0)
        if self._worker.is_alive():
            log.warning("Scene frame egress did not stop within its shutdown budget")
        if subscriber is not None:
            subscriber.close()

    def _configure_locked(self, width: int, height: int) -> None:
        try:
            self._subscriber = self._create_subscriber(width, height)
        except FrameChannelUnavailableError:
            log.info("Native media-window frame transport is unavailable")

    def _create_subscriber(self, width: int, height: int) -> _FrameSubscriber:
        raise NotImplementedError

    def _prepare_payload(self, frame: VideoFrame) -> object | None:
        raise NotImplementedError

    def _run(self) -> None:
        next_poll_at = 0.0
        while True:
            failed_subscriber: _FrameSubscriber | None = None
            with self._condition:
                if self._closed:
                    return
                subscriber = self._subscriber
                if subscriber is None:
                    self._condition.wait()
                    continue
                remaining = next_poll_at - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                try:
                    frame = subscriber.read_latest()
                except Exception:  # noqa: BLE001 - native frame boundary
                    log.warning("Could not read the native media-window frame", exc_info=True)
                    frame = None
                    if self._subscriber is subscriber:
                        self._subscriber = None
                        failed_subscriber = subscriber
            if failed_subscriber is not None:
                try:
                    failed_subscriber.close()
                except OSError:
                    log.debug("Could not close failed scene frame transport", exc_info=True)
                self.descriptor_changed.emit(None)
            if frame is not None:
                payload = self._prepare_payload(frame)
                if payload is not None:
                    self.frame_ready.emit(payload)
            next_poll_at = time.monotonic() + _POLL_INTERVAL_SECONDS


class SceneFrameEgressController(_SceneFrameEgressController):
    """Deliver fixed BGRA scene frames as detached images."""

    def _create_subscriber(
        self,
        width: int,
        height: int,
    ) -> SharedMemoryBgraFrameSubscriber:
        return SharedMemoryBgraFrameSubscriber(width, height)

    def _prepare_payload(self, frame: VideoFrame) -> QImage | None:
        image = _bgra_frame_image(frame)
        return None if image.isNull() else image


class SceneVideoFrameEgressController(_SceneFrameEgressController):
    """Deliver dynamic native frames without invoking Qt Multimedia off-thread."""

    def _create_subscriber(
        self,
        width: int,
        height: int,
    ) -> SharedMemoryVideoFrameSubscriber:
        return SharedMemoryVideoFrameSubscriber(width, height)

    def _prepare_payload(self, frame: VideoFrame) -> VideoFrame:
        return frame


def video_frame_to_image(frame: VideoFrame) -> QImage:
    """Convert a transported frame on the receiving Qt thread."""

    if frame.pixel_format is VideoPixelFormat.BGRA:
        return _bgra_frame_image(frame)
    if frame.pixel_format is not VideoPixelFormat.NV12:
        return QImage()
    frame_format = QVideoFrameFormat(
        QSize(frame.width, frame.height),
        QVideoFrameFormat.PixelFormat.Format_NV12,
    )
    frame_format.setColorSpace(QVideoFrameFormat.ColorSpace.ColorSpace_BT709)
    frame_format.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    video_frame = QVideoFrame(frame_format)
    if not video_frame.map(QVideoFrame.MapMode.WriteOnly):
        return QImage()
    try:
        source = memoryview(frame.pixels)
        y_size = frame.width * frame.height
        for plane, rows, source_offset in (
            (0, frame.height, 0),
            (1, frame.height // 2, y_size),
        ):
            target = video_frame.bits(plane)
            target_stride = video_frame.bytesPerLine(plane)
            for row in range(rows):
                row_start = source_offset + row * frame.width
                target_start = row * target_stride
                target[target_start : target_start + frame.width] = source[
                    row_start : row_start + frame.width
                ]
    finally:
        video_frame.unmap()
    return video_frame.toImage()


def _bgra_frame_image(frame: VideoFrame) -> QImage:
    if frame.pixel_format is not VideoPixelFormat.BGRA:
        return QImage()
    # PySide retains the immutable Python buffer for the lifetime of the QImage's
    # shared data. The frame-channel read already produced the owning bytes object,
    # so a second full-frame copy here only burns memory bandwidth. Qt Quick treats
    # preview images as read-only and keeps the shared data alive while uploading it.
    return QImage(
        frame.pixels,
        frame.width,
        frame.height,
        frame.width * 4,
        QImage.Format.Format_ARGB32,
    )
