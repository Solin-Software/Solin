from __future__ import annotations

import sys

import pytest

from solin.controllers.scene_frame_egress_controller import (
    SceneFrameEgressController,
    SceneVideoFrameEgressController,
)
from solin.core.scenes.engine import FrameChannelTransport
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
