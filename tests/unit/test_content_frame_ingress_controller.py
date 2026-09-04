from __future__ import annotations

import threading
import time
from collections.abc import Buffer

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QColor, QImage

from solin.controllers.content_frame_ingress_controller import (
    ContentFrameIngressController,
    _owned_frame,
)
from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.model import VideoColorRange, VideoColorSpace, VideoPixelFormat
from solin.core.projection.image_framing import ImageTransform


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


class _ImageSource:
    """A minimal object exposing ``.toImage()`` — the only non-QImage input
    ``_owned_frame`` still accepts."""

    def __init__(self, image: QImage) -> None:
        self._image = image

    def toImage(self) -> QImage:
        return self._image


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


def test_content_ingress_applies_image_transform_before_its_pixels() -> None:
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
    controller.set_image_transform(
        ImageTransform(2.0, 0.0, 0.0),
        media_epoch=1,
        canvas_width=2,
        canvas_height=2,
        animate=False,
    )
    controller.begin_presentation(1)
    controller.submit_frame(QImage(2, 2, QImage.Format.Format_ARGB32))

    assert publishers[0].published.wait(1.0)
    # The transform for the epoch is delivered before the epoch's first frame.
    assert publishers[0].image_transforms[0][0] == ImageTransform(2.0, 0.0, 0.0)
    assert publishers[0].media_epochs == [1]
    assert len(publishers) == 1
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


def test_content_ingress_materializes_toimage_source_before_worker_handoff() -> None:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))

    owned = _owned_frame(_ImageSource(image))

    assert isinstance(owned, QImage)
    assert not owned.isNull()
    assert owned.pixelColor(0, 0) == QColor("#112233")


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
