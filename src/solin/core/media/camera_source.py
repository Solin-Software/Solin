"""Per-platform libobs camera capture source specification.

Shared by the projector (``program_driver.show_camera``) and the virtual camera
so both open a camera the same way — and, on the same device, the same source
instance can be reused rather than opening the device twice.
"""

from __future__ import annotations

import base64
import binascii
import logging
import sys

log = logging.getLogger(__name__)

#: Capture backend and device-list property used by the platform plugin.
#: ``(source id, device-list property)`` per platform. The property name differs
#: because each capture source names its device setting differently — the same
#: key :func:`camera_source_spec` writes.
_ENUM_SOURCE_ID = {
    "win32": ("dshow_input", "video_device_id"),
    "linux": ("v4l2_input", "device_id"),
}


def v4l2_fourcc(pixel_format: str) -> int:
    """Pack a V4L2 fourcc string (e.g. ``"MJPG"``) into its ``__u32`` code."""
    padded = (pixel_format + "    ")[:4]
    return sum(ord(char) << (8 * index) for index, char in enumerate(padded))


def camera_source_spec(
    device_path: str,
    device_name: str = "",
    *,
    pixel_format: str = "",
    width: int = 0,
    height: int = 0,
    fps_numerator: int = 0,
    fps_denominator: int = 1,
    media_type: str = "",
    native_format: str = "",
) -> tuple[str, dict]:
    """Encode an exact mode or explicit defaults for the OBS 32.2.2 plugin.

    Pixel tokens are native: decimal DShow VideoFormat (not OBS VideoFormat),
    an AVFoundation subtype plus its native format identity, and a V4L2 FourCC on Linux.
    Every controlled mode key is present so ``Source.update`` can reset a
    previous manual selection without inheriting stale user settings.
    Saved macOS modes without a native identity are resolved from device
    metadata; missing or ambiguous matches never select a different mode.
    """
    components = (width, height, fps_numerator, fps_denominator)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF
        for value in components
    ):
        raise ValueError("Invalid camera dimensions or frame rate")
    automatic = (
        width == height == fps_numerator == 0 and fps_denominator == 1
        and not media_type and not pixel_format and not native_format
    )
    if not automatic and not (
        width > 0 and height > 0 and fps_numerator > 0 and fps_denominator > 0
        and media_type in {"video/x-raw", "image/jpeg", "video/x-h264"}
        and isinstance(pixel_format, str) and pixel_format
    ):
        raise ValueError("Camera mode must be fully automatic or exact")

    if sys.platform == "win32":
        # Discovery returns the complete name:path identifier; old plain device
        # paths still need a friendly name when supplied by a caller.
        vid = device_path if ":" in device_path or not device_name else f"{device_name}:{device_path}"
        settings = {
            "video_device_id": vid, "last_video_device_id": vid,
            "res_type": 0, "resolution": "", "frame_interval": -1, "video_format": 0,
        }
        if not automatic:
            if not pixel_format.isascii() or not pixel_format.isdecimal():
                raise ValueError("DirectShow camera format must be a native enum value")
            video_format = int(pixel_format)
            if not 1 < video_format <= 0x7FFFFFFF:
                raise ValueError("DirectShow camera format must be explicit")
            # Round rational seconds to the nearest DirectShow 100 ns unit.
            interval = (10_000_000 * fps_denominator + fps_numerator // 2) // fps_numerator
            if interval <= 0:
                raise ValueError("Camera frame interval is below DirectShow precision")
            settings.update(res_type=1, resolution=f"{width}x{height}",
                            frame_interval=interval, video_format=video_format)
        return "dshow_input", settings
    if sys.platform == "darwin":
        settings = {
            "device": device_path, "use_preset": True,
            "preset": "AVCaptureSessionPresetHigh", "supported_format": "",
            "frame_rate": {"numerator": 0, "denominator": 1},
        }
        if not automatic:
            if not native_format:
                if len(pixel_format) != 4 or not pixel_format.isascii():
                    raise ValueError("AVFoundation camera format must be a native four-character code")
                from solin.core.scenes.avfoundation_camera_discovery import resolve_avfoundation_format
                from solin.core.scenes.engine import LocalVideoFormat
                from solin.core.scenes.model import CameraMediaType

                native_format = resolve_avfoundation_format(device_path, LocalVideoFormat(
                    CameraMediaType(media_type), pixel_format, width, height,
                    fps_numerator, fps_denominator,
                ))
            try:
                decoded = base64.b64decode(native_format, validate=True).decode("ascii")
                size, ranges, colorspace, subtype = decoded.split(" ")
                subtype_bytes = (int(subtype) & 0xffffffff).to_bytes(4, "big")
                expected_pixel = (subtype_bytes.decode("ascii") if all(32 <= byte < 127 for byte in subtype_bytes)
                                  else f"0x{int(subtype) & 0xffffffff:08x}")
                if (len(native_format) > 4096 or size != f"{width}x{height}" or not ranges
                        or int(colorspace) not in {0, 1, 2, 3, 4, 5}
                        or pixel_format != expected_pixel):
                    raise ValueError("Mismatched native format")
            except (TypeError, ValueError, UnicodeError, binascii.Error) as error:
                raise ValueError("AVFoundation requires an advertised native format identity") from error
            settings.update(use_preset=False, supported_format=native_format,
                frame_rate={"numerator": fps_numerator, "denominator": fps_denominator})
        return "macos-avcapture", settings
    if not sys.platform.startswith("linux"):
        raise ValueError(f"Unsupported camera platform: {sys.platform}")
    settings = {"device_id": device_path, "pixelformat": -1, "resolution": -1, "framerate": -1}
    if not automatic:
        if not 1 <= len(pixel_format) <= 4 or not pixel_format.isascii():
            raise ValueError("V4L2 camera format must be a native four-character code")
        settings.update(
            pixelformat=v4l2_fourcc(pixel_format),
            resolution=(width << 32) | height,
            # V4L2 expects time per frame, the reciprocal of frames per second.
            framerate=(fps_denominator << 32) | fps_numerator,
        )
    return "v4l2_input", settings


def libobs_cameras() -> list[tuple[str, str]]:
    """Read plugin device IDs, releasing properties even if enumeration fails.

    An unavailable backend raises; an empty list means a successful inventory
    with no devices. Discovery of Linux devices uses the separate V4L2 adapter.
    """
    if sys.platform == "darwin":
        from solin.core.scenes.avfoundation_camera_discovery import discover_avfoundation_cameras

        return [(device.display_name, device.device_id) for device in discover_avfoundation_cameras()]
    entry = next(
        (v for prefix, v in _ENUM_SOURCE_ID.items() if sys.platform.startswith(prefix)),
        None,
    )
    if entry is None:
        return []
    source_id, list_property = entry
    try:
        from .obs_runtime import obs_runtime

        obs_runtime().ensure_started()
        from pylibobs import Properties  # type: ignore[import-not-found]

        props = Properties.from_source_id(source_id)
        try:
            cameras: list[tuple[str, str]] = []
            for prop in props:
                if prop.name != list_property:
                    continue
                for item in prop.items or ():
                    name, value = item.name.strip(), item.value
                    if name and isinstance(value, str) and value and not item.disabled:
                        cameras.append((name, value))
                break
            else:
                raise RuntimeError(f"Missing device property {list_property!r}")
        finally:
            props.release()
    except Exception as error:  # noqa: BLE001 - native plugin boundary
        raise RuntimeError(f"Camera enumeration failed for {source_id}") from error
    log.info("libobs reports %d camera(s)", len(cameras))
    return cameras


def camera_target(option) -> tuple[str, str]:
    """Map a selected camera option to ``(device_path, device_name)`` for capture.

    Returns ``("", "")`` when there is no usable physical camera to open — none
    selected, a *virtual* camera (e.g. Solin's own vcam or OBS's — never feed the
    vcam its own output), or an option with no native device path. Callers pass the
    result straight to a source builder / :meth:`VirtualCamera.set_meeting_camera`.
    """
    if option is None:
        return "", ""
    device_path = getattr(option, "device_path", "") or ""
    if not device_path or getattr(option, "is_virtual", False):
        return "", ""
    return device_path, getattr(option, "name", "") or ""


__all__ = ["camera_source_spec", "camera_target", "libobs_cameras"]
