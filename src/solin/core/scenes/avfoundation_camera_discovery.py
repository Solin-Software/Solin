"""AVFoundation device metadata without capture sessions or device configuration.

The OBS 32.2 mac-avcapture plugin identifies formats by dimensions, all frame
rate ranges, colorspace and subtype, encoded as base64. This adapter implements
that versioned native setting contract, separately from the pixel format label.
"""
from __future__ import annotations

import base64
import logging
from importlib import import_module
from fractions import Fraction
from typing import Any

from .engine import (
    MAXIMUM_LOCAL_CAMERA_CAPABILITIES, MAXIMUM_LOCAL_CAMERA_DEVICES,
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus,
    LocalVideoFormat, LocalVideoFrameInterval, LocalVideoFrameRateRange,
)
from .model import CameraMediaType

log = logging.getLogger(__name__)
_USUAL_FPS = tuple(map(Fraction, (60, 50, 30, 25, 24, 20, 15, 10, 5, 1))) + (
    Fraction(60000, 1001), Fraction(30000, 1001), Fraction(24000, 1001),
)


class AVFoundationDiscoveryError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _duration(time: Any) -> LocalVideoFrameInterval:
    if not int(time.flags) & 1 or int(time.flags) & (4 | 8 | 16) or int(time.epoch) != 0:
        raise ValueError("Camera frame duration must be finite and numeric")
    value = Fraction(int(time.value), int(time.timescale))
    return LocalVideoFrameInterval(value.numerator, value.denominator)


def _colorspace(description: Any, core_media: Any) -> int:
    # video_colorspace values used by OBSAVCapture.colorspaceFromDescription.
    extensions = core_media.CMFormatDescriptionGetExtensions(description) or {}
    matrix = extensions.get("CVImageBufferYCbCrMatrix")
    if matrix == "ITU_R_601_4":
        return 1
    if matrix == "ITU_R_709_2":
        return 2
    if matrix == "ITU_R_2020":
        transfer = extensions.get("CVImageBufferTransferFunction")
        return {"SMPTE_ST_2084_PQ": 4, "ITU_R_2100_HLG": 5}.get(str(transfer), 0)
    return 0


def _format_metadata(native: Any, core_media: Any):
    description = native.formatDescription()
    dimensions = core_media.CMVideoFormatDescriptionGetDimensions(description)
    width, height = int(dimensions.width), int(dimensions.height)
    subtype = int(core_media.CMFormatDescriptionGetMediaSubType(description))
    raw_subtype = subtype.to_bytes(4, "big")
    pixel_format = (raw_subtype.decode("ascii") if all(32 <= byte < 127 for byte in raw_subtype)
                    else f"0x{subtype:08x}")
    # Capture's normal path negotiates conversion to BGRA when the native
    # subtype cannot be delivered directly; keep the actual device subtype.
    ranges = tuple(native.videoSupportedFrameRateRanges())
    if len(ranges) > MAXIMUM_LOCAL_CAMERA_CAPABILITIES:
        raise AVFoundationDiscoveryError("capability_overflow")
    sorted_ranges = sorted(ranges, key=lambda item: (item.maxFrameRate(), item.minFrameRate()))
    description = "".join(f"{item.minFrameRate():.3f}-{item.maxFrameRate():.3f}" for item in sorted_ranges)
    signed_subtype = subtype if subtype < 0x80000000 else subtype - 0x100000000
    token = base64.b64encode(
        f"{width}x{height} {description} {_colorspace(native.formatDescription(), core_media)} {signed_subtype}".encode()
    ).decode("ascii")
    media_type = (CameraMediaType.JPEG if pixel_format in {"jpeg", "mjpa", "mjpb", "MJPG"}
                  else CameraMediaType.H264 if pixel_format in {"avc1", "H264"} else CameraMediaType.RAW)
    return media_type, pixel_format, width, height, token, ranges


def _device_metadata(device: Any, core_media: Any, permission: str, requested_size=None) -> LocalCameraDevice:
    formats: dict[LocalVideoFormat, None] = {}
    rates: dict[LocalVideoFrameRateRange, None] = {}
    error_code = permission
    try:
        native_formats = device.formats()
        if len(native_formats) > MAXIMUM_LOCAL_CAMERA_CAPABILITIES:
            raise AVFoundationDiscoveryError("capability_overflow")
        for native in native_formats:
            media, pixel, width, height, token, native_ranges = _format_metadata(native, core_media)
            if requested_size is not None and (width, height) != requested_size:
                continue
            try:
                LocalVideoFormat(media, pixel, width, height, 1, 1, token)
            except ValueError:
                continue  # Advertised modes outside the application budget.
            for bounds in native_ranges:
                item = LocalVideoFrameRateRange(media, pixel, width, height,
                    _duration(bounds.minFrameDuration()), _duration(bounds.maxFrameDuration()),
                    native_format=token)
                rates[item] = None
                candidates = (*_USUAL_FPS, 1 / item.min_interval.seconds, 1 / item.max_interval.seconds)
                for fps in dict.fromkeys(candidates):
                    if item.supports(fps.numerator, fps.denominator):
                        formats[LocalVideoFormat(media, pixel, width, height,
                            fps.numerator, fps.denominator, token)] = None
                if max(len(formats), len(rates)) > MAXIMUM_LOCAL_CAMERA_CAPABILITIES:
                    raise AVFoundationDiscoveryError("capability_overflow")
    except Exception as error:  # noqa: BLE001 - a device failure must preserve inventory
        formats.clear()
        rates.clear()
        error_code = getattr(error, "error_code", "capability_query_failed")
        log.warning("AVFoundation camera capabilities unavailable", exc_info=True)
    if permission:
        formats.clear()
        rates.clear()
    if not formats and not rates:
        error_code = error_code or "no_formats"
    state = LocalCameraProbe(
        LocalCameraProbeStatus.UNVERIFIED if error_code else LocalCameraProbeStatus.READY,
        "avfoundation", "authorization" if permission else "capabilities" if error_code else "", error_code,
    )
    # CoreMediaIO transport identity distinguishes virtual cameras without
    # matching names, localized labels, or vendor-specific model strings.
    software = int(device.transportType()) == int.from_bytes(b"virt", "big")
    return LocalCameraDevice(str(device.uniqueID()), str(device.localizedName()),
        software, tuple(formats), state, frame_rate_ranges=tuple(rates))


def discover_avfoundation_cameras(query: LocalCameraModeQuery | None = None) -> list[LocalCameraDevice]:
    """Read inventory and capabilities; never request permission or start capture."""
    # PyObjC exposes framework symbols dynamically rather than via static stubs.
    av: Any = import_module("AVFoundation")
    core_media: Any = import_module("CoreMedia")
    objc: Any = import_module("objc")

    with objc.autorelease_pool():
        status = av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeVideo)
        permission = "permission_denied" if status in (av.AVAuthorizationStatusDenied, av.AVAuthorizationStatusRestricted) else ""
        types = [av.AVCaptureDeviceTypeBuiltInWideAngleCamera,
                 getattr(av, "AVCaptureDeviceTypeExternal", av.AVCaptureDeviceTypeExternalUnknown)]
        continuity = getattr(av, "AVCaptureDeviceTypeContinuityCamera", None)
        bundle_info = objc.lookUpClass("NSBundle").mainBundle().infoDictionary() or {}
        if continuity is not None and bundle_info.get("NSCameraUseContinuityCameraDeviceType", False):
            types.append(continuity)
        session = av.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            types, av.AVMediaTypeVideo, av.AVCaptureDevicePositionUnspecified)
        devices = session.devices()
        if len(devices) > MAXIMUM_LOCAL_CAMERA_DEVICES:
            raise AVFoundationDiscoveryError("inventory_overflow")
        if not devices and permission:
            raise AVFoundationDiscoveryError(permission)
        if query is not None:
            devices = [device for device in devices if device.uniqueID() == query.device_id]
            if not devices:
                raise AVFoundationDiscoveryError("device_unavailable")
        return [_device_metadata(device, core_media, permission,
                None if query is None else (query.width, query.height)) for device in devices]
