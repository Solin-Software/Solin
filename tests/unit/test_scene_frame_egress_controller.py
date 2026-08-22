from __future__ import annotations

import gc
import sys

import pytest

from solin.controllers.scene_frame_egress_controller import (
    SceneFrameEgressController,
    SceneVideoFrameEgressController,
    _bgra_frame_image,
)
from solin.core.scenes.engine import FrameChannelTransport
from solin.core.scenes.frame_channel import VideoFrame
from solin.core.scenes.model import VideoPixelFormat


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory transport")
def test_qimage_egress_uses_a_fixed_bgra_transport() -> None:
    controller = SceneFrameEgressController(16, 16)
    try:
        descriptor = controller.descriptor
        assert descriptor is not None
        assert descriptor.transport is FrameChannelTransport.SHARED_MEMORY_BGRA
        assert descriptor.pixel_format is VideoPixelFormat.BGRA
    finally:
        controller.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shared-memory transport")
def test_video_egress_keeps_dynamic_frames_raw_for_the_qt_receiver() -> None:
    controller = SceneVideoFrameEgressController(16, 16)
    try:
        descriptor = controller.descriptor
        assert descriptor is not None
        assert descriptor.transport is FrameChannelTransport.SHARED_MEMORY_VIDEO
        assert descriptor.pixel_format is VideoPixelFormat.DYNAMIC
    finally:
        controller.close()


def test_qimage_egress_retains_the_owning_bgra_bytes_without_a_second_copy() -> None:
    pixels = bytes(range(16))
    frame = VideoFrame(
        sequence=1,
        presentation_timestamp_ns=0,
        duration_ns=16_666_667,
        produced_monotonic_ns=0,
        media_epoch=0,
        width=2,
        height=2,
        pixel_format=VideoPixelFormat.BGRA,
        pixels=pixels,
    )
    references_before = sys.getrefcount(pixels)

    image = _bgra_frame_image(frame)

    assert not image.isNull()
    assert sys.getrefcount(pixels) > references_before
    del frame
    del pixels
    gc.collect()
    # Exercise the shared QImage data after the VideoFrame and its explicit bytes
    # reference have gone away. PySide's retained buffer must remain authoritative.
    assert bytes(image.constBits()[: image.sizeInBytes()]) == bytes(range(16))
