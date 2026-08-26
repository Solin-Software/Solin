from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoSink

import solin.core.scenes.qt_media_bridge as qt_media_bridge
from solin.core.projection.image_framing import ImageTransform
from solin.core.scenes.engine import FrameChannelTransport
from solin.core.scenes.model import VideoPixelFormat
from solin.core.scenes.qt_media_bridge import (
    QtMediaBridgePublisher,
    QtMediaBridgeSubmitResult,
    QtMediaBridgeUnavailableError,
    load_qt_media_bridge,
)


@dataclass
class _NativeBridge:
    submit_result: str = "accepted"
    route_states: list[tuple[int, bool, bool]] = field(default_factory=list)
    decoder_frame_gates: list[tuple[int, bool]] = field(default_factory=list)
    direct_submission_states: list[tuple[bool, int]] = field(default_factory=list)
    media_epochs: list[int] = field(default_factory=list)
    transforms: list[tuple[object, ...]] = field(default_factory=list)
    submissions: list[tuple[QVideoFrame, int]] = field(default_factory=list)
    image_submissions: list[tuple[QImage, int]] = field(default_factory=list)
    bound_sink: QVideoSink | None = None
    closed: bool = False

    def create(self, width: int, height: int, generation: int, /) -> object:
        assert (width, height, generation) == (1920, 1080, 1)
        return object()

    def descriptor(self, bridge: object, /) -> dict[str, object]:
        return {
            "channel_id": "gpu-channel",
            "handle_token": "gpu-token",
            "generation": 1,
            "width": 1920,
            "height": 1080,
        }

    def status(self, bridge: object, /) -> dict[str, object]:
        return {
            "available": not self.closed,
            "published_sequence": 1,
            "resource_generation": 2,
            "resource_width": 1280,
            "resource_height": 720,
        }

    def set_route_state(
        self,
        bridge: object,
        session_id: int,
        accepting_frames: bool,
        demanded: bool,
        /,
    ) -> None:
        self.route_states.append((session_id, accepting_frames, demanded))

    def stage_media_epoch(self, bridge: object, media_epoch: int, /) -> None:
        self.media_epochs.append(media_epoch)

    def set_decoder_frame_gate(
        self,
        bridge: object,
        playback_session_id: int,
        accepting_frames: bool,
        /,
    ) -> None:
        self.decoder_frame_gates.append((playback_session_id, accepting_frames))

    def set_direct_submission(
        self,
        bridge: object,
        enabled: bool,
        maximum_fps: int,
        /,
    ) -> None:
        self.direct_submission_states.append((enabled, maximum_fps))

    def set_image_transform(self, bridge: object, *values: object) -> None:
        self.transforms.append(values)

    def submit(
        self,
        bridge: object,
        frame: QVideoFrame,
        session_id: int,
        /,
    ) -> str:
        self.submissions.append((frame, session_id))
        return self.submit_result

    def submit_image(
        self,
        bridge: object,
        image: QImage,
        session_id: int,
        /,
    ) -> str:
        self.image_submissions.append((image, session_id))
        return self.submit_result

    def bind_video_sink(
        self,
        bridge: object,
        sink: QVideoSink,
        /,
    ) -> None:
        self.bound_sink = sink

    def unbind_video_sink(self, bridge: object, /) -> None:
        self.bound_sink = None

    def close(self, bridge: object, /) -> None:
        self.closed = True


def test_publisher_exposes_d3d11_descriptor_and_routes_original_frame() -> None:
    native = _NativeBridge()
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)
    frame = QVideoFrame(QImage(2, 2, QImage.Format.Format_ARGB32))

    publisher.begin_session(7, enabled=True)
    result = publisher.submit(frame, session_id=7)

    assert publisher.descriptor.transport is FrameChannelTransport.D3D11_SHARED_TEXTURE
    assert publisher.descriptor.pixel_format is VideoPixelFormat.DYNAMIC
    assert publisher.descriptor.handle_token == "gpu-token"
    assert native.route_states == [(0, True, True), (7, True, True)]
    assert native.decoder_frame_gates == [(0, False)]
    assert native.direct_submission_states == [(False, 30)]
    assert native.submissions == [(frame, 7)]
    assert result is QtMediaBridgeSubmitResult.ACCEPTED
    assert publisher.status()["resource_generation"] == 2
    publisher.close()
    assert native.closed


def test_publisher_routes_static_images_through_the_same_gpu_channel() -> None:
    native = _NativeBridge()
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)
    image = QImage(1280, 720, QImage.Format.Format_ARGB32)

    publisher.begin_session(8, enabled=True)
    result = publisher.submit_image(image, session_id=8)

    assert native.image_submissions == [(image, 8)]
    assert result is QtMediaBridgeSubmitResult.ACCEPTED
    publisher.close()


def test_publisher_forwards_decoder_gate_and_direct_submission_policy() -> None:
    native = _NativeBridge()
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)

    publisher.set_decoder_frame_gate(4, accepting_frames=True)
    publisher.set_direct_submission(True, maximum_fps=24)

    assert native.decoder_frame_gates == [(0, False), (4, True)]
    assert native.direct_submission_states == [(False, 30), (True, 24)]
    publisher.close()


def test_publisher_forwards_epoch_and_signed_image_transform() -> None:
    native = _NativeBridge()
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)

    publisher.stage_media_epoch(4)
    publisher.set_image_transform(
        ImageTransform(2.0, 0.25, -0.1),
        media_epoch=4,
        canvas_width=1920,
        canvas_height=1080,
        animate=True,
        duration_ms=200,
    )

    assert native.media_epochs == [4]
    assert native.transforms == [(True, True, 4, 1920, 1080, 200, 2.0, 0.25, -0.1)]
    publisher.close()


def test_publisher_detaches_decoder_sink_before_native_shutdown() -> None:
    native = _NativeBridge()
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)
    sink = QVideoSink()

    publisher.bind_video_sink(sink)
    assert native.bound_sink is sink

    publisher.close()
    assert native.bound_sink is None
    assert native.closed


def test_publisher_rejects_invalid_native_result() -> None:
    native = _NativeBridge(submit_result="invented")
    publisher = QtMediaBridgePublisher(1920, 1080, native_module=native)

    with pytest.raises(RuntimeError, match="invalid result"):
        publisher.submit(QVideoFrame(), session_id=0)

    publisher.close()


def test_loader_is_platform_gated_before_touching_native_module() -> None:
    with pytest.raises(QtMediaBridgeUnavailableError, match="Windows"):
        load_qt_media_bridge(platform="linux")


def test_loader_rejects_a_mismatched_qt_runtime(monkeypatch) -> None:
    monkeypatch.setattr(qt_media_bridge, "qVersion", lambda: "6.11.2")

    with pytest.raises(QtMediaBridgeUnavailableError, match="pinned PySide and Qt"):
        load_qt_media_bridge(platform="win32")
