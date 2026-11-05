"""Tests for libobs-engine camera discovery (v4l2 scan + serialization).

The ioctl scan itself is hardware-specific, but the fourcc/media-type mapping,
the wire serialization, and explicit inventory failures are platform-neutral.
"""
from __future__ import annotations

import errno
import sys
from types import SimpleNamespace

import pytest

from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraModeQuery,
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
    with pytest.raises(ValueError, match="Unsupported"):
        media_type_for_fourcc("HEVC")
    with pytest.raises(ValueError, match="Unsupported"):
        media_type_for_fourcc("JPEG")
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
                   "width": 640, "height": 480, "fps_numerator": 30, "fps_denominator": 1, "native_format": ""}


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


def test_dispatcher_propagates_inventory_failure(monkeypatch):
    from solin.core.scenes import v4l2_camera_discovery as discovery

    monkeypatch.setattr(discovery.sys, "platform", "linux")
    def fail():
        raise OSError("driver unavailable")
    monkeypatch.setattr(discovery, "discover_v4l2_cameras", fail)
    with pytest.raises(OSError, match="driver unavailable"):
        discover_local_cameras()


def _driver(monkeypatch, *, stepped=False, interval_error=False):
    from solin.core.scenes import v4l2_camera_discovery as discovery

    abi = discovery._build_v4l2()
    calls = []
    fourcc = int.from_bytes(b"MJPG", "little")
    def ioctl(fd, request, entry):
        if entry.index:
            raise OSError(errno.EINVAL, "end of list")
        if request == abi["requests"]["ENUM_FMT"]:
            entry.pixelformat = fourcc
        elif request == abi["requests"]["ENUM_FRAMESIZES"]:
            entry.type = 3 if stepped else 1
            if stepped:
                native = entry.u.stepwise
                native.min_width, native.max_width, native.step_width = 640, 1920, 16
                native.min_height, native.max_height, native.step_height = 480, 1080, 8
            else:
                entry.u.discrete.width, entry.u.discrete.height = 1920, 1080
        elif request == abi["requests"]["ENUM_FRAMEINTERVALS"]:
            calls.append((entry.width, entry.height))
            if interval_error:
                raise OSError(errno.EIO, "driver failure")
            if stepped:
                entry.type = 3
                native = entry.u.stepwise
                # Resolution-specific capability: HD15, VGA30.
                denominator = 15 if entry.width > 640 else 30
                native.min.numerator, native.min.denominator = 1, denominator
                native.max.numerator, native.max.denominator = 1, 5
                native.step.numerator, native.step.denominator = 1, denominator
            else:
                entry.type = 1
                entry.u.discrete.numerator, entry.u.discrete.denominator = 1001, 30000
        return 0
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
    return abi, calls


def test_v4l2_uses_actual_reduced_frame_intervals(monkeypatch):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, calls = _driver(monkeypatch)
    formats, resolutions, rates = _probe_formats(5, abi)
    assert len(formats) == 1
    assert (formats[0].fps_numerator, formats[0].fps_denominator) == (30000, 1001)
    assert calls == [(1920, 1080)]
    assert resolutions == rates == ()


def test_v4l2_size_ranges_probe_rates_independently_per_resolution(monkeypatch):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, calls = _driver(monkeypatch, stepped=True)
    formats, resolutions, rates = _probe_formats(5, abi)
    assert resolutions[0].supports(1280, 720)
    assert len(set(calls)) == len(calls)
    assert (640, 480) in calls and (1920, 1080) in calls
    assert any(mode.width == 640 and mode.fps_numerator == 30 for mode in formats)
    assert not any(mode.width > 640 and mode.frames_per_second > 15 for mode in formats)
    assert all((rate.width, rate.height) in calls for rate in rates)
    assert not any(rate.width == 1920 and rate.supports(30, 1) for rate in rates)


def test_v4l2_interval_error_does_not_invent_nominal_fps(monkeypatch):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, _ = _driver(monkeypatch, interval_error=True)
    with pytest.raises(OSError):
        _probe_formats(5, abi)


@pytest.mark.parametrize("interval_type", [2, 3], ids=["continuous", "stepwise"])
def test_v4l2_rate_bounds_above_budget_keep_valid_choices_and_native_range(monkeypatch, interval_type):
    from fractions import Fraction

    from solin.core.scenes import v4l2_camera_discovery as discovery

    abi, _ = _driver(monkeypatch)
    native_ioctl = sys.modules["fcntl"].ioctl
    def ioctl(fd, request, entry):
        if request == abi["requests"]["ENUM_FRAMEINTERVALS"] and entry.index == 0:
            entry.type = interval_type
            native = entry.u.stepwise
            native.min.numerator, native.min.denominator = 1, 120
            native.max.numerator, native.max.denominator = 1, 5
            native.step.numerator, native.step.denominator = 1, 120
            return 0
        return native_ioctl(fd, request, entry)
    monkeypatch.setattr(sys.modules["fcntl"], "ioctl", ioctl)
    formats, resolutions, rates = discovery._probe_formats(5, abi)
    assert resolutions == ()
    assert formats and all(mode.frames_per_second <= 60 for mode in formats)
    assert any(mode.frames_per_second == 30 for mode in formats)
    assert any(mode.frames_per_second == 5 for mode in formats)
    assert rates[0].min_interval.seconds == Fraction(1, 120)
    assert rates[0].max_interval.seconds == Fraction(1, 5)
    assert not rates[0].supports(120, 1)
    if interval_type == 2:
        assert any((mode.fps_numerator, mode.fps_denominator) == (30000, 1001) for mode in formats)
    else:
        assert rates[0].step_interval.seconds == Fraction(1, 120)
    device = LocalCameraDevice("camera", "Camera", False, formats,
                               LocalCameraProbe(LocalCameraProbeStatus.READY, "v4l2"),
                               frame_rate_ranges=rates)
    assert device.probe.status is LocalCameraProbeStatus.READY


def test_v4l2_overflow_is_explicit(monkeypatch):
    from solin.core.scenes import v4l2_camera_discovery as discovery

    abi, _ = _driver(monkeypatch)
    monkeypatch.setattr(discovery, "_MAX_CAPABILITIES", 0)
    with pytest.raises(discovery.CameraCapabilityOverflowError):
        discovery._probe_formats(5, abi)


def test_v4l2_skips_metadata_retains_failed_capture_and_closes_fds(monkeypatch):
    import os

    from solin.core.scenes import v4l2_camera_discovery as discovery

    abi, _ = _driver(monkeypatch, interval_error=True)
    native_ioctl = sys.modules["fcntl"].ioctl
    closed = []
    def ioctl(fd, request, entry):
        if request == abi["requests"]["QUERYCAP"]:
            entry.capabilities = discovery._V4L2_CAP_DEVICE_CAPS
            entry.device_caps = 0 if fd == 1 else discovery._V4L2_CAP_VIDEO_CAPTURE
            entry.card, entry.driver = b"Camera", b"uvcvideo"
            return 0
        return native_ioctl(fd, request, entry)
    monkeypatch.setattr(sys.modules["fcntl"], "ioctl", ioctl)
    monkeypatch.setattr(discovery.glob, "glob", lambda _pattern: ["/dev/video1", "/dev/video2"])
    monkeypatch.setattr(os, "O_NONBLOCK", 0x800, raising=False)
    monkeypatch.setattr(os, "open", lambda path, _flags: int(path[-1]))
    monkeypatch.setattr(os, "close", closed.append)
    devices = discovery.discover_v4l2_cameras()
    assert [device.device_id for device in devices] == ["/dev/video2"]
    assert devices[0].formats == ()
    assert devices[0].probe.error_code == "capability_query_failed"
    assert closed == [1, 2]


def test_v4l2_kernel_abi_layout():
    import ctypes

    from solin.core.scenes.v4l2_camera_discovery import _build_v4l2

    abi = _build_v4l2()
    assert ctypes.sizeof(abi["capability"]) == 104
    assert ctypes.sizeof(abi["fmtdesc"]) == 64
    assert ctypes.sizeof(abi["frmsizeenum"]) == 44
    assert ctypes.sizeof(abi["frmivalenum"]) == 52


def test_custom_size_queries_its_own_intervals_only(monkeypatch):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, calls = _driver(monkeypatch, stepped=True)
    formats, resolutions, rates = _probe_formats(5, abi, requested_size=(1296, 728))
    assert calls == [(1296, 728)]
    assert resolutions == ()
    assert formats and rates
    assert all((mode.width, mode.height) == (1296, 728) for mode in (*formats, *rates))
    assert not any(mode.frames_per_second > 15 for mode in formats)


@pytest.mark.parametrize("size", [(1297, 728), (1296, 729), (1936, 1080), (320, 240)])
def test_custom_size_outside_driver_grid_never_queries_intervals(monkeypatch, size):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, calls = _driver(monkeypatch, stepped=True)
    assert _probe_formats(5, abi, requested_size=size) == ((), (), ())
    assert calls == []


def test_custom_size_excludes_other_discrete_sizes(monkeypatch):
    from solin.core.scenes.v4l2_camera_discovery import _probe_formats

    abi, calls = _driver(monkeypatch)
    assert _probe_formats(5, abi, requested_size=(1280, 720)) == ((), (), ())
    assert calls == []


@pytest.mark.parametrize("interval_error", [False, True])
def test_exact_size_adapter_closes_device_and_retains_capability_failure(monkeypatch, interval_error):
    import os

    from solin.core.scenes import v4l2_camera_discovery as discovery

    abi, calls = _driver(monkeypatch, stepped=True, interval_error=interval_error)
    native_ioctl = sys.modules["fcntl"].ioctl
    closed = []
    def ioctl(fd, request, entry):
        if request == abi["requests"]["QUERYCAP"]:
            entry.capabilities = discovery._V4L2_CAP_VIDEO_CAPTURE
            entry.card, entry.driver = b"Camera", b"uvcvideo"
            return 0
        return native_ioctl(fd, request, entry)
    monkeypatch.setattr(sys.modules["fcntl"], "ioctl", ioctl)
    monkeypatch.setattr(discovery.glob, "glob", lambda _pattern: ["/dev/video2"])
    monkeypatch.setattr(os, "O_NONBLOCK", 0x800, raising=False)
    monkeypatch.setattr(os, "open", lambda *_args: 5)
    monkeypatch.setattr(os, "close", closed.append)
    result = discovery.query_v4l2_camera_modes(LocalCameraModeQuery("/dev/video2", 1296, 728))
    assert closed == [5]
    assert calls == [(1296, 728)]
    assert result.device_id == "/dev/video2"
    assert result.resolution_ranges == ()
    assert result.probe.error_code == ("capability_query_failed" if interval_error else "")
    assert bool(result.formats) is not interval_error


def test_exact_size_adapter_rejects_missing_device_without_open(monkeypatch):
    import os

    from solin.core.scenes import v4l2_camera_discovery as discovery

    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace())
    monkeypatch.setattr(discovery.glob, "glob", lambda _pattern: [])
    def unexpected_open(*_args):
        pytest.fail("Missing device must not be opened")
    monkeypatch.setattr(os, "open", unexpected_open)
    with pytest.raises(OSError, match="inventory"):
        discovery.query_v4l2_camera_modes(LocalCameraModeQuery("/dev/video2", 1296, 728))
