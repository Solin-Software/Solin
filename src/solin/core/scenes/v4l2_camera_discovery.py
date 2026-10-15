"""Metadata-only discovery through safe V4L2 ioctls or OBS properties.

Linux avoids the capture plugin's unsafe enumeration of metadata nodes. Other
platforms query the capture plugin's properties without opening a source.
"""

from __future__ import annotations

import errno
import glob
import logging
import sys
from ctypes import Structure
from dataclasses import asdict
from fractions import Fraction
from typing import Protocol, cast

from solin.core.scenes.engine import (
    MAXIMUM_LOCAL_CAMERA_CAPABILITIES, MAXIMUM_LOCAL_CAMERA_DEVICES,
    LocalCameraModeQuery,
    LocalCameraDevice, LocalCameraProbe, LocalCameraProbeStatus, LocalVideoFormat,
    LocalVideoFrameInterval, LocalVideoFrameRateRange, LocalVideoResolutionRange,
)
from solin.core.scenes.model import CameraMediaType

log = logging.getLogger(__name__)
_MAX_CAPABILITIES = MAXIMUM_LOCAL_CAMERA_CAPABILITIES
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_DEVICE_CAPS = 0x80000000
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_DISCRETE, _CONTINUOUS, _STEPWISE = 1, 2, 3
# OBS 32.1.2's v4l2-helpers.h conversion table and two encoded inputs.
_SUPPORTED_FOURCC = frozenset({
    "YVYU", "YUYV", "UYVY", "NV12", "YU12", "YV12", "XR24", "AR24", "BGR3", "MJPG", "H264",
})
_USUAL_SIZES = ((320, 240), (640, 480), (800, 600), (1024, 768), (1280, 720),
                (1280, 960), (1920, 1080), (1920, 1200), (2560, 1440), (3840, 2160))
_USUAL_FPS = (Fraction(60), Fraction(60000, 1001), Fraction(50), Fraction(30),
              Fraction(30000, 1001), Fraction(25), Fraction(24), Fraction(24000, 1001),
              Fraction(20), Fraction(15), Fraction(10), Fraction(5), Fraction(1))


class CameraCapabilityOverflowError(RuntimeError):
    """The device metadata exceeds the bounded capability contract."""


class _V4l2Ioctl(Protocol):
    def ioctl(self, fd: int, request: int, arg: Structure, /) -> int: ...


def _build_v4l2():
    """Build the kernel ABI's structures, including the interval union."""
    import ctypes

    u32 = ctypes.c_uint32

    class Capability(ctypes.Structure):
        _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
                    ("bus_info", ctypes.c_char * 32), ("version", u32),
                    ("capabilities", u32), ("device_caps", u32), ("reserved", u32 * 3)]

    class Format(ctypes.Structure):
        _fields_ = [("index", u32), ("type", u32), ("flags", u32),
                    ("description", ctypes.c_char * 32), ("pixelformat", u32), ("reserved", u32 * 4)]

    class Size(ctypes.Structure):
        _fields_ = [("width", u32), ("height", u32)]

    class SizeRange(ctypes.Structure):
        _fields_ = [("min_width", u32), ("max_width", u32), ("step_width", u32),
                    ("min_height", u32), ("max_height", u32), ("step_height", u32)]

    class SizeUnion(ctypes.Union):
        _fields_ = [("discrete", Size), ("stepwise", SizeRange)]

    class SizeEnum(ctypes.Structure):
        _fields_ = [("index", u32), ("pixel_format", u32), ("type", u32),
                    ("u", SizeUnion), ("reserved", u32 * 2)]

    class Rational(ctypes.Structure):
        _fields_ = [("numerator", u32), ("denominator", u32)]

    class IntervalRange(ctypes.Structure):
        _fields_ = [("min", Rational), ("max", Rational), ("step", Rational)]

    class IntervalUnion(ctypes.Union):
        _fields_ = [("discrete", Rational), ("stepwise", IntervalRange)]

    class IntervalEnum(ctypes.Structure):
        _fields_ = [("index", u32), ("pixel_format", u32), ("width", u32),
                    ("height", u32), ("type", u32), ("u", IntervalUnion), ("reserved", u32 * 2)]

    def request(direction, number, kind):
        value = (direction << 30) | (ctypes.sizeof(kind) << 16) | (ord("V") << 8) | number
        return value - 0x100000000 if value >= 0x80000000 else value

    return {"capability": Capability, "fmtdesc": Format, "frmsizeenum": SizeEnum,
            "frmivalenum": IntervalEnum,
            "requests": {"QUERYCAP": request(2, 0, Capability), "ENUM_FMT": request(3, 2, Format),
                         "ENUM_FRAMESIZES": request(3, 74, SizeEnum),
                         "ENUM_FRAMEINTERVALS": request(3, 75, IntervalEnum)}}


def _fourcc_to_str(value: int) -> str:
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


def media_type_for_fourcc(fourcc: str) -> CameraMediaType:
    if fourcc.upper() not in _SUPPORTED_FOURCC:
        raise ValueError("Unsupported V4L2 camera pixel format")
    if fourcc.upper() == "MJPG":
        return CameraMediaType.JPEG
    if fourcc.upper() == "H264":
        return CameraMediaType.H264
    return CameraMediaType.RAW


def _entries(fd, v4l2, kind, request_name, **fields):
    import fcntl

    ioctl = cast(_V4l2Ioctl, fcntl).ioctl
    for index in range(_MAX_CAPABILITIES + 1):
        entry = v4l2[kind]()
        entry.index = index
        for name, value in fields.items():
            setattr(entry, name, value)
        try:
            ioctl(fd, v4l2["requests"][request_name], entry)
        except OSError as error:
            if error.errno == errno.EINVAL:
                return
            raise
        if index == _MAX_CAPABILITIES:
            raise CameraCapabilityOverflowError("Camera capability enumeration exceeded its limit")
        yield entry


def _interval(value) -> LocalVideoFrameInterval:
    fraction = Fraction(int(value.numerator), int(value.denominator))
    return LocalVideoFrameInterval(fraction.numerator, fraction.denominator)


def _probe_intervals(fd, pixel_format, fourcc, width, height, v4l2):
    formats: list[LocalVideoFormat] = []
    ranges: list[LocalVideoFrameRateRange] = []
    media_type = media_type_for_fourcc(fourcc)
    for entry in _entries(fd, v4l2, "frmivalenum", "ENUM_FRAMEINTERVALS",
                          pixel_format=pixel_format, width=width, height=height):
        if entry.type == _DISCRETE:
            interval = _interval(entry.u.discrete)
            try:
                formats.append(LocalVideoFormat(media_type, fourcc, width, height,
                                                interval.denominator, interval.numerator))
            except ValueError:
                continue  # Driver mode outside the application's capture budget.
        elif entry.type in (_CONTINUOUS, _STEPWISE):
            native = entry.u.stepwise
            rates = LocalVideoFrameRateRange(
                media_type, fourcc, width, height, _interval(native.min), _interval(native.max),
                _interval(native.step) if entry.type == _STEPWISE else None,
            )
            ranges.append(rates)
            candidates = (*_USUAL_FPS, 1 / rates.min_interval.seconds, 1 / rates.max_interval.seconds)
            for fps in dict.fromkeys(candidates):
                if rates.supports(fps.numerator, fps.denominator):
                    formats.append(LocalVideoFormat(media_type, fourcc, width, height,
                                                    fps.numerator, fps.denominator))
            break
        else:
            raise ValueError("Invalid V4L2 frame interval type")
    return formats, ranges


def _probe_formats(fd: int, v4l2, *, requested_size: tuple[int, int] | None = None):
    formats: list[LocalVideoFormat] = []
    resolutions: list[LocalVideoResolutionRange] = []
    rates: list[LocalVideoFrameRateRange] = []
    probed: set[tuple[int, int, int]] = set()
    for desc in _entries(fd, v4l2, "fmtdesc", "ENUM_FMT", type=_V4L2_BUF_TYPE_VIDEO_CAPTURE):
        fourcc = _fourcc_to_str(desc.pixelformat)
        if fourcc not in _SUPPORTED_FOURCC:
            continue
        for entry in _entries(fd, v4l2, "frmsizeenum", "ENUM_FRAMESIZES", pixel_format=desc.pixelformat):
            if entry.type == _DISCRETE:
                sizes = [(int(entry.u.discrete.width), int(entry.u.discrete.height))]
                if requested_size is not None and sizes[0] != requested_size:
                    sizes = []
            elif entry.type in (_CONTINUOUS, _STEPWISE):
                native = entry.u.stepwise
                size_range = LocalVideoResolutionRange(
                    media_type_for_fourcc(fourcc), fourcc,
                    int(native.min_width), int(native.max_width),
                    int(native.step_width) if entry.type == _STEPWISE else 1,
                    int(native.min_height), int(native.max_height),
                    int(native.step_height) if entry.type == _STEPWISE else 1,
                )
                if requested_size is None:
                    resolutions.append(size_range)
                    candidates = (*_USUAL_SIZES, (size_range.min_width, size_range.min_height),
                                  (size_range.max_width, size_range.max_height))
                else:
                    candidates = (requested_size,)
                sizes = [size for size in dict.fromkeys(candidates) if size_range.supports(*size)]
            else:
                raise ValueError("Invalid V4L2 frame size type")
            for width, height in sizes:
                identity = (int(desc.pixelformat), width, height)
                if identity in probed:
                    continue
                probed.add(identity)
                try:
                    LocalVideoFormat(media_type_for_fourcc(fourcc), fourcc, width, height, 1, 1)
                except ValueError:
                    continue
                choices, intervals = _probe_intervals(fd, desc.pixelformat, fourcc, width, height, v4l2)
                formats.extend(choices)
                rates.extend(intervals)
                if max(len(formats), len(rates), len(resolutions)) > _MAX_CAPABILITIES:
                    raise CameraCapabilityOverflowError("Camera capability result exceeded its limit")
            if entry.type != _DISCRETE:
                break
    return tuple(dict.fromkeys(formats)), tuple(dict.fromkeys(resolutions)), tuple(dict.fromkeys(rates))


def discover_v4l2_cameras() -> list[LocalCameraDevice]:
    """Retain discovered devices when only their capability query fails."""
    import fcntl
    import os

    ioctl = cast(_V4l2Ioctl, fcntl).ioctl
    nonblocking = getattr(os, "O_NONBLOCK", None)
    if not isinstance(nonblocking, int):
        raise OSError("V4L2 discovery requires nonblocking POSIX device access")
    v4l2 = _build_v4l2()
    devices: list[LocalCameraDevice] = []
    failures: list[OSError] = []
    for path in sorted(glob.glob("/dev/video*")):
        try:
            fd = os.open(path, os.O_RDONLY | nonblocking)
        except OSError as error:
            failures.append(error)
            continue
        try:
            cap = v4l2["capability"]()
            ioctl(fd, v4l2["requests"]["QUERYCAP"], cap)
            caps = cap.device_caps if cap.capabilities & _V4L2_CAP_DEVICE_CAPS else cap.capabilities
            if not caps & _V4L2_CAP_VIDEO_CAPTURE:
                continue
            card = cap.card.decode("utf-8", "replace").strip() or path
            software = "loopback" in (card + cap.driver.decode("utf-8", "replace")).lower()
            formats, resolutions, rates, error_code = (), (), (), ""
            try:
                formats, resolutions, rates = _probe_formats(fd, v4l2)
            except CameraCapabilityOverflowError:
                error_code = "capability_overflow"
            except (OSError, ValueError, ZeroDivisionError):
                error_code = "capability_query_failed"
                log.warning("V4L2 capability query failed for %s", path, exc_info=True)
            if not (formats or resolutions or rates):
                error_code = error_code or "no_formats"
            probe = LocalCameraProbe(
                LocalCameraProbeStatus.UNVERIFIED if error_code else LocalCameraProbeStatus.READY,
                "v4l2", "capabilities" if error_code else "", error_code,
            )
            devices.append(LocalCameraDevice(path, card, software, formats, probe, resolutions, rates))
        except OSError as error:
            failures.append(error)
        finally:
            os.close(fd)
    if not devices and failures:
        raise OSError("Unable to enumerate V4L2 capture devices") from failures[0]
    if len(devices) > MAXIMUM_LOCAL_CAMERA_DEVICES:
        raise CameraCapabilityOverflowError("Camera inventory exceeded its limit")
    return devices


def discover_local_cameras() -> list[LocalCameraDevice]:
    """Return inventory or raise on enumeration failure, never conflate the two."""
    if sys.platform.startswith("linux"):
        return discover_v4l2_cameras()
    from solin.core.media.obs_camera_properties import discover_obs_cameras

    return discover_obs_cameras()


def query_v4l2_camera_modes(query: LocalCameraModeQuery) -> LocalCameraDevice:
    """Read intervals only after checking this size against the live size grid."""
    import fcntl
    import os

    if query.device_id not in glob.glob("/dev/video*"):
        raise OSError(errno.ENODEV, "Camera is no longer in the device inventory")
    nonblocking = getattr(os, "O_NONBLOCK", None)
    if not isinstance(nonblocking, int):
        raise OSError("V4L2 discovery requires nonblocking POSIX device access")
    v4l2 = _build_v4l2()
    fd = os.open(query.device_id, os.O_RDONLY | nonblocking)
    try:
        cap = v4l2["capability"]()
        cast(_V4l2Ioctl, fcntl).ioctl(fd, v4l2["requests"]["QUERYCAP"], cap)
        caps = cap.device_caps if cap.capabilities & _V4L2_CAP_DEVICE_CAPS else cap.capabilities
        if not caps & _V4L2_CAP_VIDEO_CAPTURE:
            raise OSError(errno.ENODEV, "Device is not a V4L2 capture camera")
        name = cap.card.decode("utf-8", "replace").strip() or query.device_id
        software = "loopback" in (name + cap.driver.decode("utf-8", "replace")).lower()
        formats, rates, error_code = (), (), ""
        try:
            formats, _resolutions, rates = _probe_formats(
                fd, v4l2, requested_size=(query.width, query.height),
            )
            if not formats and not rates:
                error_code = "no_formats"
        except CameraCapabilityOverflowError:
            error_code = "capability_overflow"
        except (OSError, ValueError, ZeroDivisionError):
            error_code = "capability_query_failed"
            log.warning("V4L2 exact-size capability query failed", exc_info=True)
        state = LocalCameraProbe(
            LocalCameraProbeStatus.UNVERIFIED if error_code else LocalCameraProbeStatus.READY,
            "v4l2", "capabilities" if error_code else "", error_code,
        )
        return LocalCameraDevice(query.device_id, name, software, formats, state, frame_rate_ranges=rates)
    finally:
        os.close(fd)


def query_local_camera_modes(query: LocalCameraModeQuery) -> LocalCameraDevice:
    """Return metadata for exactly the requested device and resolution."""
    if not isinstance(query, LocalCameraModeQuery):
        raise TypeError("Camera mode query must be a LocalCameraModeQuery")
    if sys.platform.startswith("linux"):
        return query_v4l2_camera_modes(query)
    from solin.core.media.obs_camera_properties import query_obs_camera_modes

    return query_obs_camera_modes(query)


def serialize_camera_device(device: LocalCameraDevice) -> dict:
    """The complete versioned local_camera_list device wire shape."""
    record = asdict(device)
    for mode in record["formats"]:
        mode["media_type"] = mode["media_type"].value
    record["formats"] = list(record["formats"])
    for key in ("resolution_ranges", "frame_rate_ranges"):
        record[key] = list(record[key])
        for item in record[key]:
            item["media_type"] = item["media_type"].value
    record["probe"]["status"] = device.probe.status.value
    return record
