from __future__ import annotations

import threading
import time
from collections.abc import Buffer
from concurrent.futures import ThreadPoolExecutor

import pytest

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QColor, QImage, QPixmap

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


def test_default_ingress_publishes_cross_platform_content(monkeypatch) -> None:
    from solin.core.scenes.content_frame_channel import SharedFrameChannelReader

    monkeypatch.setattr("sys.platform", "linux")
    controller = ContentFrameIngressController(canvas_width=2, canvas_height=2)
    try:
        descriptor = controller.descriptor
        assert descriptor is not None
        assert descriptor.transport is FrameChannelTransport.SHARED_MEMORY_BGRA
        reader = SharedFrameChannelReader(descriptor.handle_token, 2, 2)
        try:
            image = QImage(2, 2, QImage.Format.Format_ARGB32)
            image.fill(QColor("red"))
            controller.submit_frame(image)
            frames = []

            def received() -> bool:
                frame = reader.read_latest()
                if frame is not None:
                    frames.append(frame)
                return bool(frames)

            assert _wait_for(received)
            assert frames[0].data == bytes(image.constBits())
        finally:
            reader.close()
    finally:
        controller.close()


def test_ingress_retries_channel_creation_after_resource_failure() -> None:
    from solin.core.scenes.content_frame_publisher import SharedMemoryContentPublisher

    attempts = []

    def create_publisher(width, height):
        attempts.append((width, height))
        if len(attempts) == 1:
            raise OSError("Shared memory could not be allocated")
        return SharedMemoryContentPublisher(width, height)

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher, canvas_width=2, canvas_height=2,
    )
    try:
        assert controller.descriptor is None
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(QColor("red"))
        controller.submit_frame(image)
        assert _wait_for(lambda: controller.descriptor is not None)
        assert attempts == [(2, 2), (2, 2)]
    finally:
        controller.close()


def test_ingress_rejects_worker_construction_before_acquiring_resources() -> None:
    calls = []
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(
            ContentFrameIngressController,
            publisher_factory=lambda *args: calls.append(args),
        )
        with pytest.raises(RuntimeError, match="created on the main Qt thread"):
            result.result(timeout=2)
    assert calls == []


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
        media_epoch: int = 0,
    ) -> int:
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
        media_epoch: int = 0,
    ) -> int:
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("bounded frame channel is busy")
        return super().publish(
            pixels,
            frame_width=frame_width,
            frame_height=frame_height,
            media_epoch=media_epoch,
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


def test_content_ingress_keeps_future_transform_out_of_the_current_epoch() -> None:
    publisher = _Publisher(8, 4, 1)
    controller = ContentFrameIngressController(
        publisher_factory=lambda *_: publisher, maximum_fps=60,
        canvas_width=8, canvas_height=4,
    )
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        controller.begin_presentation(1)
        controller.submit_frame(image)
        assert _wait_for(lambda: len(publisher.frames) == 1)
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=2,
            canvas_width=8, canvas_height=4, animate=False,
        )
        controller.submit_frame(image)
        assert _wait_for(lambda: len(publisher.frames) >= 2)
        assert publisher.frame_sizes[-1] == (4, 4)
        assert publisher.media_epochs[-1] == 1

        controller.begin_presentation(2)
        controller.submit_frame(image)
        assert _wait_for(lambda: publisher.media_epochs[-1] == 2)
        assert publisher.frame_sizes[-1] == (8, 4)
        assert publisher.frames[-1] == bytes((0, 0, 255, 255)) * 32
    finally:
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
    publisher = _Publisher(8, 4, 1)
    controller = ContentFrameIngressController(
        publisher_factory=lambda *_: publisher, maximum_fps=60,
        canvas_width=8, canvas_height=4,
    )
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=1,
            canvas_width=8, canvas_height=4, animate=False,
        )
        controller.begin_presentation(1)
        controller.submit_frame(image)
        assert publisher.published.wait(1.0)
        assert publisher.frame_sizes == [(8, 4)]
        assert publisher.frames == [bytes((0, 0, 255, 255)) * 32]
        assert publisher.media_epochs == [1]
    finally:
        controller.close()


def test_new_epoch_animation_starts_without_the_previous_epochs_zoom() -> None:
    from solin.core.projection.transform_animation import ProjectionTransformAnimation

    publisher = _Publisher(8, 4, 1)
    controller = ContentFrameIngressController(
        publisher_factory=lambda *_: publisher, maximum_fps=60,
        canvas_width=8, canvas_height=4,
    )
    controller._animation = ProjectionTransformAnimation(clock=lambda: 0.0)
    sleeping = threading.Event()
    wait = controller._condition.wait

    def observe_wait(timeout=None):
        if timeout is None:
            sleeping.set()
        return wait(timeout)

    controller._condition.wait = observe_wait
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        controller.begin_presentation(1)
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=1,
            canvas_width=8, canvas_height=4, animate=False,
        )
        controller.submit_frame(image)
        assert _wait_for(lambda: bool(publisher.frames))
        assert publisher.frame_sizes[0] == (8, 4)
        with controller._condition:
            sleeping.clear()
            controller._condition.notify()
        assert sleeping.wait(1.0)
        # Let the previous frame pacing deadline expire while the worker sleeps.
        time.sleep(0.03)

        # Wake the sleeping worker with the target, epoch and pixels together.
        with controller._condition:
            controller.set_image_transform(
                ImageTransform(1.5, 0.0, 0.0), media_epoch=2,
                canvas_width=8, canvas_height=4, animate=True,
            )
            controller.begin_presentation(2)
            controller.submit_frame(image)
        assert _wait_for(lambda: 2 in publisher.media_epochs)
        first = publisher.media_epochs.index(2)
        assert publisher.frame_sizes[first] == (4, 4)
        assert publisher.frames[first] == bytes(image.constBits())
    finally:
        controller.close()


def test_channel_recreation_announces_descriptor_after_epoch_cancellation() -> None:
    publisher = _Publisher(2, 2, 1)
    creating = threading.Event()
    release = threading.Event()
    attempts = []

    def create_publisher(*dimensions):
        attempts.append(dimensions)
        if len(attempts) == 1:
            raise OSError("Allocation temporarily unavailable")
        creating.set()
        assert release.wait(2.0)
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher, canvas_width=2, canvas_height=2,
    )
    descriptors = []
    controller.descriptor_changed.connect(descriptors.append)
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        controller.submit_frame(image)
        assert creating.wait(1.0)
        controller.begin_presentation(1)
        controller.submit_frame(image)
        release.set()
        assert _wait_for(lambda: bool(publisher.frames))
        assert publisher.media_epochs == [1]
        assert _wait_for(lambda: descriptors == [publisher.descriptor])
    finally:
        release.set()
        controller.close()


def test_transform_recreates_channel_and_announces_it_before_pixels() -> None:
    publisher = _Publisher(8, 4, 1)
    attempts = []

    def create_publisher(*dimensions):
        attempts.append(dimensions)
        if len(attempts) == 1:
            raise OSError("Allocation temporarily unavailable")
        return publisher

    controller = ContentFrameIngressController(
        publisher_factory=create_publisher, canvas_width=8, canvas_height=4,
    )
    descriptors = []
    controller.descriptor_changed.connect(descriptors.append)
    try:
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=0,
            canvas_width=8, canvas_height=4, animate=False,
        )
        assert _wait_for(lambda: descriptors == [publisher.descriptor])
        assert controller.descriptor == publisher.descriptor
        assert publisher.frames == []
        assert attempts == [(8, 4), (8, 4)]
    finally:
        controller.close()


def test_epoch_change_cancels_pixels_being_transformed(monkeypatch) -> None:
    from solin.controllers import content_frame_ingress_controller as ingress

    publisher = _Publisher(8, 4, 1)
    rendering = threading.Event()
    release = threading.Event()
    render = ingress._framed_for_canvas

    def block_render(*args):
        rendering.set()
        assert release.wait(2.0)
        return render(*args)

    monkeypatch.setattr(ingress, "_framed_for_canvas", block_render)
    controller = ContentFrameIngressController(
        publisher_factory=lambda *_: publisher, canvas_width=8, canvas_height=4,
    )
    first = QImage(4, 4, QImage.Format.Format_ARGB32)
    first.fill(QColor("red"))
    second = QImage(4, 4, QImage.Format.Format_ARGB32)
    second.fill(QColor("blue"))
    try:
        controller.begin_presentation(1)
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=1,
            canvas_width=8, canvas_height=4, animate=False,
        )
        controller.submit_frame(first)
        assert rendering.wait(1.0)
        controller.begin_presentation(2)
        controller.submit_frame(second)
        release.set()
        assert _wait_for(lambda: bool(publisher.frames))
        assert publisher.media_epochs == [2]
        assert publisher.frame_sizes == [(4, 4)]
        assert publisher.frames == [bytes(second.constBits())]
    finally:
        release.set()
        controller.close()


def test_animated_transform_redraws_retained_pixels_until_the_target(monkeypatch) -> None:
    from solin.controllers import content_frame_ingress_controller as ingress
    from solin.core.projection.transform_animation import ProjectionTransformAnimation

    now = [0.0]
    monkeypatch.setattr(
        ingress, "ProjectionTransformAnimation",
        lambda: ProjectionTransformAnimation(duration_seconds=1.0, clock=lambda: now[0]),
    )
    publisher = _Publisher(8, 4, 1)
    controller = ContentFrameIngressController(
        publisher_factory=lambda *_: publisher, maximum_fps=60,
        canvas_width=8, canvas_height=4,
    )
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    try:
        controller.begin_presentation(1)
        controller.submit_frame(image)
        assert _wait_for(lambda: len(publisher.frames) == 1)
        controller.set_image_transform(
            ImageTransform(2.0, 0.0, 0.0), media_epoch=1,
            canvas_width=8, canvas_height=4, animate=True,
        )
        assert _wait_for(lambda: len(publisher.frames) >= 2)
        now[0] = 0.1
        assert _wait_for(lambda: publisher.frame_sizes[-1] == (8, 4))
        intermediate = publisher.frames[-1]
        target = bytes((0, 0, 255, 255)) * 32
        assert intermediate != target
        now[0] = 1.0
        assert _wait_for(lambda: publisher.frames[-1] == target)
        assert set(publisher.media_epochs) == {1}
    finally:
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


def test_content_ingress_materializes_qpixmap_before_worker_handoff() -> None:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#112233"))

    owned = _owned_frame(QPixmap.fromImage(image))

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
