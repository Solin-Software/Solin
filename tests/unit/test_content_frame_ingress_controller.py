from __future__ import annotations

import threading
import time
import struct
import sys
from collections.abc import Buffer
from multiprocessing import shared_memory
from typing import cast
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QVideoSink

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
from solin.core.projection.image_framing import ImageTransform
from solin.core.scenes.qt_media_bridge import QtMediaBridgeSubmitResult


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
        self.media_epochs: list[int] = []
        self.requested_media_epochs: list[int] = []
        self.image_transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
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
        media_epoch: int = 0,
    ) -> int:
        assert presentation_timestamp_ns == 0
        assert duration_ns == 0
        assert media_epoch >= 0
        self.publish_started_at.append(time.monotonic())
        if self.publish_delay:
            time.sleep(self.publish_delay)
        assert len(memoryview(pixels)) == frame_width * frame_height * 4
        self.frames.append(bytes(memoryview(pixels)))
        self.frame_sizes.append((frame_width, frame_height))
        self.media_epochs.append(media_epoch)
        self.published.set()
        return len(self.frames)

    def set_media_epoch(self, media_epoch: int) -> None:
        assert media_epoch >= 0
        self.requested_media_epochs.append(media_epoch)

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        **options: object,
    ) -> None:
        self.image_transforms.append((transform, options))

    def close(self) -> None:
        self.closed = True


class _AcceleratedPublisher:
    def __init__(self, width: int, height: int) -> None:
        self.descriptor = FrameChannelDescriptor(
            channel_id="gpu-channel",
            generation=1,
            producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
            transport=FrameChannelTransport.D3D11_SHARED_TEXTURE,
            handle_token="gpu-token",
            width=width,
            height=height,
            pixel_format=VideoPixelFormat.NV12,
            color_space=VideoColorSpace.BT709,
            color_range=VideoColorRange.LIMITED,
        )
        self.route_states: list[tuple[int, bool]] = []
        self.decoder_frame_gates: list[tuple[int, bool]] = []
        self.direct_submission_states: list[tuple[bool, int]] = []
        self.media_epochs: list[int] = []
        self.image_transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
        self.submissions: list[tuple[QVideoFrame, int]] = []
        self.image_submissions: list[tuple[QImage, int]] = []
        self.submit_result = QtMediaBridgeSubmitResult.ACCEPTED
        self.submit_image_result = QtMediaBridgeSubmitResult.UNAVAILABLE
        self.fail_demand_update = False
        self.status_error_code = ""
        self.bound_sink: QVideoSink | None = None
        self.closed = False

    def begin_session(self, session_id: int, *, enabled: bool) -> None:
        self.route_states.append((session_id, enabled))

    def set_enabled(self, enabled: bool) -> None:
        if self.fail_demand_update:
            raise RuntimeError("accelerated ingress failed")
        self.route_states.append((self.route_states[-1][0] if self.route_states else 0, enabled))

    def set_decoder_frame_gate(
        self,
        playback_session_id: int,
        *,
        accepting_frames: bool,
    ) -> None:
        self.decoder_frame_gates.append((playback_session_id, accepting_frames))

    def set_direct_submission(self, enabled: bool, *, maximum_fps: int) -> None:
        self.direct_submission_states.append((enabled, maximum_fps))

    def status(self) -> dict[str, object]:
        return {
            "available": not self.closed,
            "error_code": self.status_error_code,
            "direct_submission_active": bool(
                self.direct_submission_states and self.direct_submission_states[-1][0]
            ),
        }

    def stage_media_epoch(self, media_epoch: int) -> None:
        self.media_epochs.append(media_epoch)

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        **options: object,
    ) -> None:
        self.image_transforms.append((transform, options))

    def submit(
        self,
        frame: QVideoFrame,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult:
        self.submissions.append((frame, session_id))
        return self.submit_result

    def submit_image(
        self,
        image: QImage,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult:
        self.image_submissions.append((QImage(image), session_id))
        return self.submit_image_result

    def bind_video_sink(self, sink: QVideoSink) -> None:
        self.bound_sink = sink

    def unbind_video_sink(self) -> None:
        self.bound_sink = None

    def close(self) -> None:
        self.closed = True


class _HardwareNv12Frame:
    def pixelFormat(self) -> QVideoFrameFormat.PixelFormat:
        return QVideoFrameFormat.PixelFormat.Format_NV12

    def handleType(self) -> QVideoFrame.HandleType:
        return QVideoFrame.HandleType.RhiTextureHandle


class _BlockingClosePublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait()
        super().close()


class _BlockingFirstPublishPublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.first_publish_started = threading.Event()
        self.allow_first_publish = threading.Event()
        self.second_publish_started = threading.Event()
        self.publish_calls = 0

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int:
        self.publish_calls += 1
        if self.publish_calls == 1:
            self.first_publish_started.set()
            self.allow_first_publish.wait()
        elif self.publish_calls == 2:
            self.second_publish_started.set()
        return super().publish(
            pixels,
            frame_width=frame_width,
            frame_height=frame_height,
            presentation_timestamp_ns=presentation_timestamp_ns,
            duration_ns=duration_ns,
            media_epoch=media_epoch,
        )


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
        media_epoch: int = 0,
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
            media_epoch=media_epoch,
        )


class _BackpressuredTransformPublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.transform_attempts = 0

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        **options: object,
    ) -> None:
        self.transform_attempts += 1
        if self.transform_attempts == 1:
            raise TimeoutError("bounded frame channel is busy")
        super().set_image_transform(transform, **options)


class _FailingPublisher(_Publisher):
    def __init__(self, width: int, height: int, generation: int) -> None:
        super().__init__(width, height, generation)
        self.publish_attempted = threading.Event()

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int,
        frame_height: int,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        media_epoch: int = 0,
    ) -> int:
        self.publish_attempted.set()
        raise RuntimeError("fallback publication failed")


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


def test_content_ingress_retries_a_backpressured_static_image_in_place() -> None:
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
    assert publishers[0].published.wait(1.0)

    controller.close()

    assert publishers[0].attempts == 2
    assert len(publishers) == 1
    assert publishers[0].closed


def test_content_ingress_does_not_publish_an_epoch_before_its_first_frame() -> None:
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
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#445566"))
    controller.submit_frame(image)
    assert publishers[0].published.wait(1.0)
    publishers[0].published.clear()

    controller.begin_presentation(7)

    assert not publishers[0].published.wait(0.05)
    controller.close()
    assert publishers[0].media_epochs == [0]
    assert publishers[0].requested_media_epochs == []


def test_content_ingress_coalesces_image_transform_state_independently_from_pixels() -> None:
    publishers: list[_Publisher] = []

    def create_publisher(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        canvas_width=320,
        canvas_height=180,
    )

    controller.set_image_transform(
        ImageTransform(2.0, 0.25, -0.1),
        media_epoch=7,
        canvas_width=320,
        canvas_height=180,
        animate=True,
    )

    assert _wait_for(lambda: bool(publishers[0].image_transforms))
    assert publishers[0].frames == []
    assert publishers[0].image_transforms == [
        (
            ImageTransform(2.0, 0.25, -0.1),
            {
                "media_epoch": 7,
                "canvas_width": 320,
                "canvas_height": 180,
                "animate": True,
                "duration_ms": 2100,
            },
        )
    ]
    controller.close()


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


def test_content_ingress_never_lets_pixels_overtake_their_image_transform() -> None:
    publishers: list[_BackpressuredTransformPublisher] = []

    def create_publisher(width: int, height: int) -> _BackpressuredTransformPublisher:
        publisher = _BackpressuredTransformPublisher(
            width,
            height,
            len(publishers) + 1,
        )
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    controller.set_image_transform(
        ImageTransform(2.0, 0.0, 0.0),
        media_epoch=1,
        canvas_width=2,
        canvas_height=2,
        animate=False,
    )
    controller.begin_presentation(1)
    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert _wait_for(lambda: publishers[0].transform_attempts == 1)
    assert publishers[0].frames == []
    assert publishers[0].published.wait(1.0)
    assert publishers[0].transform_attempts == 2
    assert publishers[0].image_transforms[0][0] == ImageTransform(2.0, 0.0, 0.0)
    assert publishers[0].media_epochs == [1]
    controller.close()


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


def test_content_ingress_drops_video_before_mapping_when_disabled() -> None:
    controller = ContentFrameIngressController(
        publisher_factory=lambda width, height: _Publisher(width, height, 1),
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    frame = QVideoFrame(QImage(2, 2, QImage.Format.Format_ARGB32))
    controller.set_enabled(False)

    with (
        patch.object(controller, "_publish_video_frame_fallback") as publish_video,
        patch("solin.controllers.content_frame_ingress_controller._owned_frame") as own_frame,
    ):
        controller.submit_frame(frame)

    publish_video.assert_not_called()
    own_frame.assert_not_called()
    controller.close()


def test_content_ingress_replays_retained_static_image_when_enabled() -> None:
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
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))
    controller.set_enabled(False)

    controller.submit_frame(image)
    assert not publishers[0].published.wait(0.05)

    controller.set_enabled(True)
    assert publishers[0].published.wait(1.0)
    assert publishers[0].frames == [bytes([0x33, 0x22, 0x11, 0xFF]) * 4]
    controller.close()


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


def test_content_ingress_starts_on_gpu_and_does_not_create_cpu_transport() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )
    frame = cast(QVideoFrame, _HardwareNv12Frame())
    controller.set_decoder_frame_gate(1, True)

    assert controller.descriptor == accelerated.descriptor
    assert controller._publish_accelerated_video_frame(frame, 0)
    assert accelerated.submissions == [(frame, 0)]
    assert fallback_publishers == []

    controller.close()
    assert accelerated.closed


def test_content_ingress_keeps_one_gpu_route_across_video_and_static_media() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    accelerated.submit_image_result = QtMediaBridgeSubmitResult.ACCEPTED
    fallback_publishers: list[_Publisher] = []

    controller = ContentFrameIngressController(
        publisher_factory=lambda width, height: fallback_publishers.append(
            _Publisher(width, height, len(fallback_publishers) + 1)
        )
        or fallback_publishers[-1],
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=1920,
        canvas_height=1080,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    image = QImage(1280, 720, QImage.Format.Format_ARGB32)
    image.fill(QColor("#234567"))

    controller.begin_presentation(7)
    controller.set_image_transform(
        None,
        media_epoch=7,
        canvas_width=1920,
        canvas_height=1080,
        animate=False,
    )
    controller.submit_frame(image)

    assert _wait_for(lambda: len(accelerated.image_submissions) == 1)
    published, media_epoch = accelerated.image_submissions[0]
    assert (published.width(), published.height(), media_epoch) == (1280, 720, 7)
    assert controller.descriptor == accelerated.descriptor
    assert fallback_publishers == []
    assert descriptors == []
    controller.close()


def test_content_ingress_retries_a_busy_accelerated_static_frame() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    accelerated.submit_image_result = QtMediaBridgeSubmitResult.DROPPED
    fallback_publishers: list[_Publisher] = []

    controller = ContentFrameIngressController(
        publisher_factory=lambda width, height: fallback_publishers.append(
            _Publisher(width, height, len(fallback_publishers) + 1)
        )
        or fallback_publishers[-1],
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        canvas_width=1920,
        canvas_height=1080,
    )
    controller.begin_presentation(8)
    image = QImage(1280, 720, QImage.Format.Format_ARGB32)
    image.fill(QColor("#23c45e"))
    controller.submit_frame(image)

    assert _wait_for(lambda: len(accelerated.image_submissions) >= 1)
    accelerated.submit_image_result = QtMediaBridgeSubmitResult.ACCEPTED
    assert _wait_for(lambda: len(accelerated.image_submissions) >= 2)
    assert controller.descriptor == accelerated.descriptor
    assert fallback_publishers == []
    controller.close()


def test_content_ingress_promotes_static_fallback_only_after_its_first_frame() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    fallback_publishers: list[_BlockingFirstPublishPublisher] = []

    def create_fallback(width: int, height: int) -> _BlockingFirstPublishPublisher:
        publisher = _BlockingFirstPublishPublisher(
            width,
            height,
            len(fallback_publishers) + 1,
        )
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    controller.begin_presentation(7)
    controller.set_image_transform(
        ImageTransform(2.0, 0.25, -0.1),
        media_epoch=7,
        canvas_width=2,
        canvas_height=2,
        animate=False,
    )

    try:
        controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))
        assert _wait_for(lambda: bool(fallback_publishers))
        fallback = fallback_publishers[0]
        assert fallback.first_publish_started.wait(1.0)

        # Keep the already-published GPU route authoritative until the complete
        # static fallback state is readable. Otherwise the engine can observe a
        # new descriptor without its first image and recover the frozen video.
        assert descriptors == []

        fallback.allow_first_publish.set()
        assert _wait_for(lambda: descriptors == [fallback.descriptor])
        assert controller.descriptor == fallback.descriptor
        assert fallback.media_epochs[-1] == 7
        assert fallback.image_transforms[-1] == (
            ImageTransform(2.0, 0.25, -0.1),
            {
                "media_epoch": 7,
                "canvas_width": 2,
                "canvas_height": 2,
                "animate": False,
                "duration_ms": 2100,
            },
        )
    finally:
        if fallback_publishers:
            fallback_publishers[0].allow_first_publish.set()
        controller.close()


def test_content_ingress_retries_static_fallback_handoff_without_channel_churn() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    fallback_publishers: list[_BackpressuredTransformPublisher] = []

    def create_fallback(width: int, height: int) -> _BackpressuredTransformPublisher:
        publisher = _BackpressuredTransformPublisher(
            width,
            height,
            len(fallback_publishers) + 1,
        )
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    controller.begin_presentation(9)
    controller.set_image_transform(
        ImageTransform(1.5, -0.2, 0.1),
        media_epoch=9,
        canvas_width=2,
        canvas_height=2,
        animate=False,
    )

    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert _wait_for(lambda: bool(fallback_publishers))
    assert fallback_publishers[0].published.wait(1.0)
    assert len(fallback_publishers) == 1
    assert fallback_publishers[0].transform_attempts == 2
    assert not fallback_publishers[0].closed
    assert _wait_for(lambda: descriptors == [fallback_publishers[0].descriptor])
    assert controller.descriptor == fallback_publishers[0].descriptor
    controller.close()


def test_content_ingress_keeps_gpu_route_when_static_fallback_preparation_fails() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    failing = _FailingPublisher(2, 2, 1)
    recovered = _Publisher(2, 2, 2)
    fallback_publishers = [failing, recovered]

    controller = ContentFrameIngressController(
        publisher_factory=lambda _width, _height: fallback_publishers.pop(0),
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )

    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert failing.publish_attempted.wait(1.0)
    assert _wait_for(lambda: failing.closed)
    assert descriptors == []
    assert controller.descriptor == accelerated.descriptor

    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))
    assert recovered.published.wait(1.0)
    assert _wait_for(lambda: descriptors == [recovered.descriptor])
    assert controller.descriptor == recovered.descriptor
    controller.close()


def test_content_ingress_reopens_static_media_across_repeated_gpu_handoffs() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
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
    image.fill(QColor("#234567"))

    for media_epoch in range(1, 6):
        controller.begin_presentation(media_epoch)
        controller.set_image_transform(
            None,
            media_epoch=media_epoch,
            canvas_width=2,
            canvas_height=2,
            animate=False,
        )
        controller.submit_frame(image)
        assert _wait_for(
            lambda expected_epoch=media_epoch: (
                bool(fallback_publishers)
                and len(fallback_publishers[0].frames) == expected_epoch
                and controller.descriptor == fallback_publishers[0].descriptor
            )
        )
        assert _wait_for(
            lambda: descriptors and descriptors[-1] == fallback_publishers[0].descriptor
        )
        if media_epoch < 5:
            controller.set_decoder_frame_gate(media_epoch, True)
            assert controller._publish_accelerated_video_frame(
                cast(QVideoFrame, _HardwareNv12Frame()),
                media_epoch,
            )
            assert controller.descriptor == accelerated.descriptor
            controller.set_decoder_frame_gate(media_epoch, False)

    assert len(fallback_publishers) == 1
    assert fallback_publishers[0].media_epochs == [1, 2, 3, 4, 5]
    assert fallback_publishers[0].requested_media_epochs == []
    assert descriptors == [
        descriptor
        for _ in range(4)
        for descriptor in (fallback_publishers[0].descriptor, accelerated.descriptor)
    ] + [fallback_publishers[0].descriptor]
    controller.close()


def test_content_ingress_rejects_late_video_without_reactivating_old_gpu_frame() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    controller.begin_presentation(1)
    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert _wait_for(
        lambda: (
            bool(fallback_publishers) and controller.descriptor == fallback_publishers[0].descriptor
        )
    )
    assert _wait_for(lambda: descriptors == [fallback_publishers[0].descriptor])
    controller.set_decoder_frame_gate(1, False)
    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        1,
    )

    assert controller.descriptor == fallback_publishers[0].descriptor
    assert accelerated.submissions == []
    assert descriptors == [fallback_publishers[0].descriptor]
    controller.close()


def test_content_ingress_promotes_gpu_route_only_after_first_frame_is_accepted() -> None:
    accelerated = _AcceleratedPublisher(2, 2)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=60,
        canvas_width=2,
        canvas_height=2,
    )
    controller.begin_presentation(1)
    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))
    assert _wait_for(
        lambda: (
            bool(fallback_publishers) and controller.descriptor == fallback_publishers[0].descriptor
        )
    )
    controller.set_decoder_frame_gate(1, True)
    accelerated.submit_result = QtMediaBridgeSubmitResult.SESSION_REJECTED

    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        1,
    )
    assert controller.descriptor == fallback_publishers[0].descriptor

    accelerated.submit_result = QtMediaBridgeSubmitResult.ACCEPTED
    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        1,
    )
    assert controller.descriptor == accelerated.descriptor
    controller.close()


def test_content_ingress_uses_warmup_before_switching_software_frames_to_fallback() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )
    software_frame = QVideoFrame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert controller._publish_accelerated_video_frame(software_frame, 0)
    assert controller._publish_accelerated_video_frame(software_frame, 0)
    assert fallback_publishers == []
    assert not controller._publish_accelerated_video_frame(software_frame, 0)
    assert fallback_publishers == []

    controller.submit_frame(software_frame.toImage())

    assert _wait_for(lambda: len(fallback_publishers) == 1)
    assert fallback_publishers[0].published.wait(1.0)
    assert _wait_for(lambda: descriptors == [fallback_publishers[0].descriptor])
    assert controller.descriptor == fallback_publishers[0].descriptor
    assert accelerated.route_states[-1] == (0, False)
    controller.close()


def test_content_ingress_recovers_from_native_demand_failure_without_propagating() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    accelerated.fail_demand_update = True
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )

    controller.set_enabled(False)

    assert accelerated.closed
    assert len(fallback_publishers) == 1
    assert controller.descriptor == fallback_publishers[0].descriptor
    controller.close()


@pytest.mark.parametrize(
    "error_code",
    [
        "content_d3d11_device_lost",
        "content_d3d11_ingress_failed",
    ],
)
def test_content_ingress_permanently_falls_back_after_engine_gpu_failure(
    error_code: str,
) -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )
    descriptors: list[FrameChannelDescriptor | None] = []
    controller.descriptor_changed.connect(
        descriptors.append,
        Qt.ConnectionType.DirectConnection,
    )

    assert not controller.recover_accelerated_transport("content_ingress_failed")
    assert controller.recover_accelerated_transport(error_code)

    assert accelerated.closed
    assert len(fallback_publishers) == 1
    assert controller.descriptor == fallback_publishers[0].descriptor
    assert descriptors == [fallback_publishers[0].descriptor]
    assert not controller.recover_accelerated_transport(error_code)
    assert len(fallback_publishers) == 1
    controller.close()


def test_content_ingress_activates_direct_submission_after_compatible_handshake() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    controller = ContentFrameIngressController(
        publisher_factory=lambda width, height: _Publisher(width, height, 1),
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
        maximum_fps=24,
    )
    states: list[bool] = []
    controller.direct_submission_changed.connect(states.append)
    controller.set_decoder_frame_gate(3, True)
    controller.begin_presentation(7)

    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        7,
    )
    QCoreApplication.processEvents()

    assert controller.direct_submission_active
    assert accelerated.decoder_frame_gates[-1] == (3, True)
    assert accelerated.direct_submission_states[-1] == (True, 24)
    assert states == [True]
    controller.close()
    assert states == [True, False]


def test_content_ingress_rearms_direct_submission_at_every_presentation_boundary() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    controller = ContentFrameIngressController(
        publisher_factory=lambda width, height: _Publisher(width, height, 1),
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )
    states: list[bool] = []
    controller.direct_submission_changed.connect(states.append)
    controller.set_decoder_frame_gate(3, True)
    controller.begin_presentation(7)
    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        7,
    )
    QCoreApplication.processEvents()
    assert controller.direct_submission_active

    controller.begin_presentation(8)

    assert not controller.direct_submission_active
    assert accelerated.direct_submission_states[-1] == (False, 30)
    assert accelerated.route_states[-1] == (8, True)
    assert accelerated.media_epochs[-1] == 8
    assert states == [True, False]

    # A queued activation from the former presentation is stale and cannot
    # reopen direct delivery. The first accepted frame of the new identity is
    # the only event that may do so.
    controller._activate_direct_submission(accelerated, 7)
    assert not controller.direct_submission_active
    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        8,
    )
    QCoreApplication.processEvents()
    assert controller.direct_submission_active
    assert states == [True, False, True]
    controller.close()


def test_content_ingress_falls_back_when_direct_bridge_reports_failure() -> None:
    accelerated = _AcceleratedPublisher(1920, 1080)
    fallback_publishers: list[_Publisher] = []

    def create_fallback(width: int, height: int) -> _Publisher:
        publisher = _Publisher(width, height, len(fallback_publishers) + 1)
        fallback_publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_fallback,
        accelerated_publisher_factory=lambda _width, _height: accelerated,
        enable_accelerated=True,
    )
    controller.set_decoder_frame_gate(1, True)
    assert controller._publish_accelerated_video_frame(
        cast(QVideoFrame, _HardwareNv12Frame()),
        0,
    )
    QCoreApplication.processEvents()
    assert controller.direct_submission_active

    accelerated.status_error_code = "d3d11_bridge_frame_incompatible"
    controller._check_direct_submission_health()

    assert not controller.direct_submission_active
    assert accelerated.closed
    assert len(fallback_publishers) == 1
    assert controller.descriptor == fallback_publishers[0].descriptor
    controller.close()


def test_content_ingress_paces_frame_starts_without_adding_processing_time() -> None:
    publishers: list[_BlockingFirstPublishPublisher] = []

    def create_publisher(width: int, height: int) -> _BlockingFirstPublishPublisher:
        publisher = _BlockingFirstPublishPublisher(width, height, len(publishers) + 1)
        publishers.append(publisher)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher,
        maximum_fps=5,
        canvas_width=2,
        canvas_height=2,
    )
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    try:
        controller.submit_frame(image)
        assert publishers[0].first_publish_started.wait(1.0)
        controller.submit_frame(image)

        # Keep the first publication busy beyond the 200 ms frame interval. A
        # start-based deadline is already overdue when it returns, whereas the
        # former completion-based clock would add another full 200 ms wait.
        time.sleep(0.25)
        publishers[0].allow_first_publish.set()
        assert publishers[0].second_publish_started.wait(0.1)
    finally:
        publishers[0].allow_first_publish.set()
        controller.close()


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
        enable_accelerated=False,
        maximum_fps=60,
        canvas_width=4,
        canvas_height=2,
    )
    controller.begin_presentation(9)
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
        assert struct.unpack_from("<Q", attached.buf, slot + 96)[0] == 9
        assert bytes(
            attached.buf[
                slot + FRAME_CHANNEL_SLOT_HEADER_SIZE : slot
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
        QCoreApplication.processEvents()
        if predicate():
            return True
        event.wait(0.005)
    QCoreApplication.processEvents()
    return predicate()
