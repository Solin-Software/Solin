"""Tests for libobs-engine camera discovery (v4l2 scan + serialization).

The ioctl scan itself is hardware-specific, but the fourcc/media-type mapping,
the wire serialization, and the never-raise contract are platform-neutral.
"""
from __future__ import annotations

from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
)
from solin.core.scenes.model import CameraMediaType
from solin.core.scenes.v4l2_camera_discovery import (
    _fourcc_to_str,
    discover_local_cameras,
    media_type_for_fourcc,
    serialize_camera_device,
)


def test_fourcc_decoding():
    # 'YUYV' little-endian packed into a u32.
    value = ord("Y") | (ord("U") << 8) | (ord("Y") << 16) | (ord("V") << 24)
    assert _fourcc_to_str(value) == "YUYV"


def test_media_type_mapping():
    assert media_type_for_fourcc("MJPG") is CameraMediaType.JPEG
    assert media_type_for_fourcc("H264") is CameraMediaType.H264
    assert media_type_for_fourcc("HEVC") is CameraMediaType.H264
    assert media_type_for_fourcc("YUYV") is CameraMediaType.RAW
    assert media_type_for_fourcc("nv12") is CameraMediaType.RAW  # case-insensitive


def _ready_device() -> LocalCameraDevice:
    return LocalCameraDevice(
        device_id="/dev/video1", display_name="Brio 105", software_device=False,
        formats=(LocalVideoFormat(
            media_type=CameraMediaType.RAW, pixel_format="YUYV",
            width=640, height=480, fps_numerator=30, fps_denominator=1),),
        probe=LocalCameraProbe(status=LocalCameraProbeStatus.READY, backend="v4l2"))


def test_serialize_ready_device_has_wire_shape():
    payload = serialize_camera_device(_ready_device())
    assert payload["device_id"] == "/dev/video1"
    assert payload["display_name"] == "Brio 105"
    assert payload["software_device"] is False
    assert payload["probe"]["status"] == "ready" and payload["probe"]["backend"] == "v4l2"
    assert len(payload["formats"]) == 1
    fmt = payload["formats"][0]
    assert fmt == {"media_type": "video/x-raw", "pixel_format": "YUYV",
                   "width": 640, "height": 480, "fps_numerator": 30, "fps_denominator": 1}


def test_serialize_unverified_device_carries_failure_fields():
    device = LocalCameraDevice(
        device_id="/dev/video9", display_name="Cam", software_device=False, formats=(),
        probe=LocalCameraProbe(status=LocalCameraProbeStatus.UNVERIFIED, backend="v4l2",
                               failure_stage="format_probe", error_code="no_formats"))
    payload = serialize_camera_device(device)
    assert payload["formats"] == []
    assert payload["probe"]["status"] == "unverified"
    assert payload["probe"]["failure_stage"] == "format_probe"
    assert payload["probe"]["error_code"] == "no_formats"


def test_discover_local_cameras_never_raises_and_returns_list():
    result = discover_local_cameras()
    assert isinstance(result, list)
    assert all(isinstance(d, LocalCameraDevice) for d in result)
