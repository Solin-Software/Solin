from __future__ import annotations

import threading
import time
import struct
import sys
from collections.abc import Buffer
from multiprocessing import shared_memory
from statistics import median
from unittest.mock import patch

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from solin.controllers.content_frame_ingress_controller import (
    ContentFrameIngressController,
    _advance_video_deadline,
    _owned_frame,
)
from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.frame_channel import FRAME_CHANNEL_SLOT_HEADER_SIZE
from solin.core.scenes.model import VideoColorRange, VideoColorSpace, VideoPixelFormat


class _Publisher:
    def __init__(
        self,
        width: int,
        height: int,
        generation: int,
        *,
        publish_delay: float = 0.0,
    ) -> None:
        self.descriptor = FrameChannelDescriptor(
            channel_id=f"channel-{generation}",
            generation=generation,
            producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
            transport=FrameChannelTransport.SHARED_MEMORY_BGRA,
            handle_token=f"mapping-{generation}",
            width=width,
            height=height,
            pixel_format=VideoPixelFormat.BGRA,
            color_space=VideoColorSpace.SRGB,
            color_range=VideoColorRange.FULL,
        )
        self.frames: list[bytes] = []
        self.frame_sizes: list[tuple[int, int]] = []
        self.publish_started_at: list[float] = []
        self.publish_delay = publish_delay
        self.published = threading.Event()
        self.closed = False

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
    ) -> int:
        assert presentation_timestamp_ns == 0
        assert duration_ns == 0
        self.publish_started_at.append(time.monotonic())
        if self.publish_delay:
            time.sleep(self.publish_delay)
        assert len(memoryview(pixels)) == frame_width * frame_height * 4
        self.frames.append(bytes(memoryview(pixels)))
        self.frame_sizes.append((frame_width, frame_height))
        self.published.set()
        return len(self.frames)

    def close(self) -> None:
        self.closed = True


class _BlockingClosePublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait()
        super().close()


class _BackpressuredPublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.attempts = 0

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
    ) -> int:
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("bounded frame channel is busy")
        return super().publish(
            pixels,
            frame_width=frame_width,
            frame_height=frame_height,
            presentation_timestamp_ns=presentation_timestamp_ns,
            duration_ns=duration_ns,
        )


def test_content_ingress_converts_qt_images_to_bounded_bgra_frames() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))

    controller.submit_frame(image)

    assert publishers or _wait_for(lambda: bool(publishers))
    assert publishers[0].published.wait(1.0)
    assert controller.descriptor == publishers[0].descriptor
    assert descriptors == []
    assert publishers[0].frames == [bytes([0x33, 0x22, 0x11, 0xFF]) * 4]
    controller.close()
    assert publishers[0].closed


def test_content_ingress_drops_backpressured_images_without_replacing_channel() -> None:
    publishers: list[_BackpressuredPublisher] = []

    def create_publisher(width: int, height: int) -> _BackpressuredPublisher:
        publisher = _BackpressuredPublisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    controller.submit_frame(image)
    assert _wait_for(lambda: publishers[0].attempts == 1)
    controller.submit_frame(image)
    assert publishers[0].published.wait(1.0)

    controller.close()

    assert len(publishers) == 1
    assert publishers[0].closed


def test_content_ingress_keeps_a_stable_channel_when_input_dimensions_change() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    first = QImage(2, 2, QImage.Format.Format_ARGB32)
    second = QImage(3, 2, QImage.Format.Format_ARGB32)
    controller.submit_frame(first)
    assert _wait_for(lambda: len(publishers) == 1 and bool(publishers[0].frames))
    controller.submit_frame(first)
    assert _wait_for(lambda: len(publishers[0].frames) == 2)
    controller.submit_frame(second)
    assert _wait_for(lambda: len(publishers[0].frames) == 3)

    controller.close()

    assert publishers[0].closed
    assert len(publishers) == 1
    assert publishers[0].frame_sizes[-1] == (2, 1)
    assert len(publishers[0].frames[-1]) == 2 * 1 * 4


def test_content_ingress_preserves_smaller_source_dimensions_for_gpu_scaling() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        canvas_width=4,
        canvas_height=4,
    )
    image = QImage(4, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))

    controller.submit_frame(image)
    assert _wait_for(lambda: bool(publishers[0].frames))
    controller.close()

    assert publishers[0].frame_sizes == [(4, 2)]
    assert len(publishers[0].frames[0]) == 4 * 2 * 4


def test_content_ingress_throttles_video_before_materializing_qt_surfaces() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=1,
        canvas_width=2,
        canvas_height=2,
    )
    frame = QVideoFrame(QImage(2, 2, QImage.Format.Format_ARGB32))
    materialized = QImage(2, 2, QImage.Format.Format_ARGB32)

    with patch(
        "solin.controllers.content_frame_ingress_controller._owned_frame",
        return_value=materialized,
    ) as own_frame:
        for _ in range(20):
            controller.submit_frame(frame)

    assert _wait_for(lambda: bool(publishers[0].frames))
    controller.close()

    own_frame.assert_called_once_with(frame)


def test_video_sampling_accepts_2997_fps_jitter_without_exceeding_30_fps() -> None:
    interval = 1 / 30
    deadline = 0.0
    accepted_2997 = 0
    for index in range(300):
        advanced = _advance_video_deadline(deadline, index / 29.97, interval)
        if advanced is not None:
            accepted_2997 += 1
            deadline = advanced

    deadline = 0.0
    accepted_60 = 0
    for index in range(600):
        advanced = _advance_video_deadline(deadline, index / 60, interval)
        if advanced is not None:
            accepted_60 += 1
            deadline = advanced

    assert accepted_2997 == 300
    assert 299 <= accepted_60 <= 301


def test_content_ingress_descriptor_exists_before_the_first_frame() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        canvas_width=4,
        canvas_height=3,
    )

    assert controller.descriptor == publishers[0].descriptor
    assert controller.descriptor.width == 4
    assert controller.descriptor.height == 3

    controller.close()


def test_content_ingress_paces_frame_starts_without_adding_processing_time() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(
            width,
            height,
            len(publishers) + 1,
            publish_delay=0.010,
        )
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=30,
        canvas_width=2,
        canvas_height=2,
    )
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    deadline = time.monotonic() + 0.55
    while time.monotonic() < deadline:
        controller.submit_frame(image)
        time.sleep(0.002)
    assert _wait_for(lambda: len(publishers[0].publish_started_at) >= 12)
    controller.close()

    intervals = [
        right - left
        for left, right in zip(
            publishers[0].publish_started_at,
            publishers[0].publish_started_at[1:],
            strict=False,
        )
    ]
    assert len(intervals) >= 11
    # The former completion-based clock produced ~43 ms here (23 fps).
    assert median(intervals) < 0.041


def test_content_ingress_shutdown_budget_does_not_join_native_worker(
    monkeypatch,
) -> None:
    publishers: list[_BlockingClosePublisher] = []

    def create_publisher(width: int, height: int) -> _BlockingClosePublisher:
        publisher = _BlockingClosePublisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    monkeypatch.setattr(
        "solin.controllers.content_frame_ingress_controller._SHUTDOWN_BUDGET_SECONDS",
        0.05,
    )
    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        canvas_width=2,
        canvas_height=2,
    )

    started = time.monotonic()
    controller.close()
    elapsed = time.monotonic() - started

    assert publishers[0].close_started.wait(0.5)
    assert elapsed < 0.25
    assert controller._worker.is_alive()

    publishers[0].allow_close.set()
    assert controller._worker_stopped.wait(0.5)
    assert publishers[0].closed


def test_content_ingress_materializes_video_frame_before_worker_handoff() -> None:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))

    owned = _owned_frame(QVideoFrame(image))

    assert isinstance(owned, QImage)
    assert not owned.isNull()
    assert owned.pixelColor(0, 0) == QColor("#112233")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory backend")
def test_content_ingress_publishes_nv12_planes_without_bgra_materialization() -> None:
    controller = ContentFrameIngressController(
        maximum_fps=60,
        canvas_width=4,
        canvas_height=2,
    )
    descriptor = controller.descriptor
    assert descriptor is not None
    assert descriptor.transport is FrameChannelTransport.SHARED_MEMORY_VIDEO
    assert descriptor.pixel_format is VideoPixelFormat.DYNAMIC
    frame = QVideoFrame(
        QVideoFrameFormat(
            QSize(4, 2),
            QVideoFrameFormat.PixelFormat.Format_NV12,
        )
    )
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    first_stride = frame.bytesPerLine(0)
    second_stride = frame.bytesPerLine(1)
    first_plane_size = first_stride + 4
    second_plane_size = 4
    frame.bits(0)[:first_plane_size] = bytes(range(first_plane_size))
    frame.bits(1)[:4] = bytes((101, 102, 103, 104))
    frame.unmap()

    controller.submit_frame(frame)

    attached = shared_memory.SharedMemory(name=descriptor.handle_token, create=False)
    try:
        sequence = struct.unpack_from("<Q", attached.buf, 56)[0]
        assert sequence == 1
        slot = 128
        assert struct.unpack_from("<QIIII", attached.buf, slot + 40) == (
            first_plane_size + second_plane_size,
            4,
            2,
            first_stride,
            2,
        )
        assert struct.unpack_from("<I", attached.buf, slot + 80)[0] == second_stride
        assert struct.unpack_from("<Q", attached.buf, slot + 88)[0] == first_plane_size
        assert bytes(
            attached.buf[
                slot + FRAME_CHANNEL_SLOT_HEADER_SIZE :
                slot
                + FRAME_CHANNEL_SLOT_HEADER_SIZE
                + first_plane_size
                + second_plane_size
            ]
        ) == bytes(range(first_plane_size)) + bytes((101, 102, 103, 104))
    finally:
        attached.close()
        controller.close()


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    event = threading.Event()
    deadline_steps = int(timeout / 0.005)
    for _ in range(deadline_steps):
        if predicate():
            return True
        event.wait(0.005)
    return predicate()
