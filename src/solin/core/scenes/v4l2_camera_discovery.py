"""Camera discovery for the libobs scene engine.

libobs' own v4l2 property enumeration (``Properties.from_source_id("v4l2_input")``)
segfaults on some device sets — notably machines whose ``/dev/video0`` is a
non-capture (metadata) node — so the sidecar cannot use it. Instead this scans
``/dev/video*`` directly with the kernel's V4L2 ioctls: it keeps only true capture
nodes (``V4L2_CAP_VIDEO_CAPTURE``), reads each card's name, and probes its pixel
formats + frame sizes. That both fixes the crash and avoids listing the bogus
``/dev/video0`` metadata node as a camera.

On non-Linux platforms it falls back to libobs' capture enumeration
(``dshow_input`` / ``av_capture_input``), which does not have the v4l2 problem.
"""

from __future__ import annotations

import glob
import logging
import sys

from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFormat,
)
from solin.core.scenes.model import CameraMediaType

log = logging.getLogger(__name__)

_MAX_FORMATS_PER_DEVICE = 48
_DEFAULT_FPS = (30, 1)  # nominal; libobs negotiates the real rate on open

# ── V4L2 ioctl definitions (Linux) ───────────────────────────────────────────

_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_DEVICE_CAPS = 0x80000000
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_FRMSIZE_TYPE_DISCRETE = 1


def _build_v4l2():
    """Construct the ctypes structs + ioctl request numbers (Linux only)."""
    import ctypes

    u32 = ctypes.c_uint32

    class v4l2_capability(ctypes.Structure):
        _fields_ = [
            ("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
            ("bus_info", ctypes.c_char * 32), ("version", u32),
            ("capabilities", u32), ("device_caps", u32), ("reserved", u32 * 3),
        ]

    class v4l2_fmtdesc(ctypes.Structure):
        _fields_ = [
            ("index", u32), ("type", u32), ("flags", u32),
            ("description", ctypes.c_char * 32), ("pixelformat", u32),
            ("reserved", u32 * 4),
        ]

    class v4l2_frmsize_discrete(ctypes.Structure):
        _fields_ = [("width", u32), ("height", u32)]

    class v4l2_frmsize_stepwise(ctypes.Structure):
        _fields_ = [
            ("min_width", u32), ("max_width", u32), ("step_width", u32),
            ("min_height", u32), ("max_height", u32), ("step_height", u32),
        ]

    class _frmsize_union(ctypes.Union):
        _fields_ = [("discrete", v4l2_frmsize_discrete),
                    ("stepwise", v4l2_frmsize_stepwise)]

    class v4l2_frmsizeenum(ctypes.Structure):
        _fields_ = [
            ("index", u32), ("pixel_format", u32), ("type", u32),
            ("u", _frmsize_union), ("reserved", u32 * 2),
        ]

    def _ioc(direction: int, nr: int, size: int) -> int:
        # asm-generic ioctl encoding: dir(2) | size(14) | type(8) | nr(8).
        value = (direction << 30) | (size << 16) | (ord("V") << 8) | nr
        # fcntl.ioctl wants a signed C int; fold values with the high bit set.
        return value - 0x100000000 if value >= 0x80000000 else value

    _READ, _WRITE = 2, 1
    requests = {
        "QUERYCAP": _ioc(_READ, 0, ctypes.sizeof(v4l2_capability)),
        "ENUM_FMT": _ioc(_READ | _WRITE, 2, ctypes.sizeof(v4l2_fmtdesc)),
        "ENUM_FRAMESIZES": _ioc(_READ | _WRITE, 74, ctypes.sizeof(v4l2_frmsizeenum)),
    }
    return {
        "capability": v4l2_capability, "fmtdesc": v4l2_fmtdesc,
        "frmsizeenum": v4l2_frmsizeenum, "requests": requests,
    }


def _fourcc_to_str(value: int) -> str:
    chars = [chr((value >> (8 * i)) & 0xFF) for i in range(4)]
    return "".join(c for c in chars if c.isprintable()).strip()


def media_type_for_fourcc(fourcc: str) -> CameraMediaType:
    upper = fourcc.upper()
    if upper in ("MJPG", "JPEG"):
        return CameraMediaType.JPEG
    if upper in ("H264", "H265", "HEVC", "AVC1"):
        return CameraMediaType.H264
    return CameraMediaType.RAW


# ── Linux v4l2 scan ───────────────────────────────────────────────────────────


def _probe_formats(fd: int, v4l2) -> list[LocalVideoFormat]:
    import ctypes
    import fcntl

    formats: list[LocalVideoFormat] = []
    seen: set[tuple] = set()
    fmt_index = 0
    while len(formats) < _MAX_FORMATS_PER_DEVICE and fmt_index < 64:
        desc = v4l2["fmtdesc"]()
        desc.index = fmt_index
        desc.type = _V4L2_BUF_TYPE_VIDEO_CAPTURE
        try:
            fcntl.ioctl(fd, v4l2["requests"]["ENUM_FMT"], desc)
        except OSError:
            break  # no more formats
        fmt_index += 1
        fourcc = _fourcc_to_str(desc.pixelformat)
        if not fourcc:
            continue
        media_type = media_type_for_fourcc(fourcc)
        for width, height in _frame_sizes(fd, desc.pixelformat, v4l2):
            key = (media_type, fourcc, width, height)
            if key in seen:
                continue
            seen.add(key)
            try:
                formats.append(LocalVideoFormat(
                    media_type=media_type, pixel_format=fourcc,
                    width=width, height=height,
                    fps_numerator=_DEFAULT_FPS[0], fps_denominator=_DEFAULT_FPS[1]))
            except ValueError:
                continue  # out-of-bounds size — skip, keep the rest
            if len(formats) >= _MAX_FORMATS_PER_DEVICE:
                break
    return formats


def _frame_sizes(fd: int, pixelformat: int, v4l2) -> list[tuple[int, int]]:
    import fcntl

    sizes: list[tuple[int, int]] = []
    index = 0
    while index < 64:
        entry = v4l2["frmsizeenum"]()
        entry.index = index
        entry.pixel_format = pixelformat
        try:
            fcntl.ioctl(fd, v4l2["requests"]["ENUM_FRAMESIZES"], entry)
        except OSError:
            break
        if entry.type != _V4L2_FRMSIZE_TYPE_DISCRETE:
            break  # stepwise/continuous — leave format negotiation to libobs
        sizes.append((int(entry.u.discrete.width), int(entry.u.discrete.height)))
        index += 1
    return sizes


def discover_v4l2_cameras() -> list[LocalCameraDevice]:
    """Enumerate Linux capture cameras via V4L2 ioctls (never the metadata nodes)."""
    import fcntl
    import os

    v4l2 = _build_v4l2()
    devices: list[LocalCameraDevice] = []
    for path in sorted(glob.glob("/dev/video*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            cap = v4l2["capability"]()
            fcntl.ioctl(fd, v4l2["requests"]["QUERYCAP"], cap)
            caps = cap.device_caps if (cap.capabilities & _V4L2_CAP_DEVICE_CAPS) else cap.capabilities
            if not (caps & _V4L2_CAP_VIDEO_CAPTURE):
                continue  # metadata/output node — not a camera
            card = cap.card.decode("utf-8", "replace").strip() or path
            driver = cap.driver.decode("utf-8", "replace").strip().lower()
            software = "loopback" in card.lower() or "loopback" in driver
            formats = _probe_formats(fd, v4l2)
            devices.append(_make_device(path, card, software, formats))
        except OSError:
            log.debug("v4l2 query failed for %s", path, exc_info=True)
        except Exception:  # noqa: BLE001 - one bad device must not sink the scan
            log.warning("v4l2 discovery error for %s", path, exc_info=True)
        finally:
            os.close(fd)
    return devices


def _make_device(
    device_id: str, display_name: str, software: bool, formats: list[LocalVideoFormat]
) -> LocalCameraDevice:
    if formats:
        probe = LocalCameraProbe(status=LocalCameraProbeStatus.READY, backend="v4l2")
    else:
        # Discovered but formats could not be read; still selectable ("Automatic").
        probe = LocalCameraProbe(
            status=LocalCameraProbeStatus.UNVERIFIED, backend="v4l2",
            failure_stage="format_probe", error_code="no_formats")
    return LocalCameraDevice(
        device_id=device_id, display_name=display_name, software_device=software,
        formats=tuple(formats), probe=probe)


# ── libobs fallback (non-Linux) ───────────────────────────────────────────────


def _discover_via_libobs() -> list[LocalCameraDevice]:
    from solin.core.media.camera_source import libobs_cameras

    devices: list[LocalCameraDevice] = []
    for name, device_id in libobs_cameras():
        if not device_id:
            continue
        devices.append(LocalCameraDevice(
            device_id=device_id, display_name=name or device_id, software_device=False,
            formats=(),
            probe=LocalCameraProbe(
                status=LocalCameraProbeStatus.UNVERIFIED, backend="libobs",
                failure_stage="discovery", error_code="not_probed")))
    return devices


def discover_local_cameras() -> list[LocalCameraDevice]:
    """Discover selectable cameras for the current platform (never raises)."""
    try:
        if sys.platform.startswith("linux"):
            return discover_v4l2_cameras()
        return _discover_via_libobs()
    except Exception:  # noqa: BLE001 - discovery is best-effort
        log.warning("local camera discovery failed", exc_info=True)
        return []


def serialize_camera_device(device: LocalCameraDevice) -> dict:
    """Serialize a device to the ``local_camera_list`` wire shape."""
    return {
        "device_id": device.device_id,
        "display_name": device.display_name,
        "software_device": device.software_device,
        "formats": [
            {
                "media_type": fmt.media_type.value,
                "pixel_format": fmt.pixel_format,
                "width": fmt.width,
                "height": fmt.height,
                "fps_numerator": fmt.fps_numerator,
                "fps_denominator": fmt.fps_denominator,
            }
            for fmt in device.formats
        ],
        "probe": {
            "status": device.probe.status.value,
            "backend": device.probe.backend,
            "failure_stage": device.probe.failure_stage,
            "error_code": device.probe.error_code,
            "native_error_code": device.probe.native_error_code,
        },
    }
