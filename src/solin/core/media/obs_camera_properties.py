"""Live OBS camera metadata, without constructing or activating capture sources.

Property lists are rebuilt by native modified callbacks. Never use pylibobs's
cached property snapshots for dependent modes. Native values are persisted
independently of translated labels and OBS's pixel enum.
"""

from __future__ import annotations

import json
import logging
import sys
from fractions import Fraction
from typing import Any

from solin.core.scenes.engine import (
    MAXIMUM_LOCAL_CAMERA_CAPABILITIES, MAXIMUM_LOCAL_CAMERA_DEVICES,
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus,
    LocalVideoFormat, LocalVideoFrameInterval, LocalVideoFrameRateRange,
)
from solin.core.scenes.model import CameraMediaType

log = logging.getLogger(__name__)
_LIMIT = MAXIMUM_LOCAL_CAMERA_CAPABILITIES
_CALLBACK_LIMIT = 65536
# dshowcapture::VideoFormat, OBS 32.1.2; these are not obs_video_format values.
_DSHOW_RAW = frozenset({100, 101, 200, 201, 202, 203, 204, 300, 301, 302, 303})
_USUAL_FPS = (Fraction(60), Fraction(60000, 1001), Fraction(50), Fraction(30),
              Fraction(30000, 1001), Fraction(25), Fraction(24), Fraction(24000, 1001),
              Fraction(20), Fraction(15), Fraction(10), Fraction(5), Fraction(1))
# The plugin's integer 100 ns intervals deliberately use truncation. Recover
# their intended rational values without parsing translated FPS display names.
_DSHOW_RATES = {10_000_000 * fps.denominator // fps.numerator: fps for fps in _USUAL_FPS}
_DSHOW_RATES[10_000_000 * 1001 // 48000] = Fraction(48000, 1001)
_DSHOW_RATES[10_000_000 // 40] = Fraction(40)
for _fps in (4, 3, 2):
    _DSHOW_RATES[10_000_000 // _fps] = Fraction(_fps)


class CameraPropertiesError(RuntimeError):
    """A capability failure for an enumerated device, not an inventory failure."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class _NativeProperties:
    """Own raw properties and temporary settings with deterministic release."""

    def __init__(self, source_id: str) -> None:
        from pylibobs._ffi import ffi, get_lib

        from .pylibobs_compat import complete_frame_rate_type_compat

        complete_frame_rate_type_compat(ffi)
        self.ffi: Any = ffi
        self.lib: Any = get_lib()
        self.properties: Any = self.ffi.NULL
        self.settings: Any = self.ffi.NULL
        self._callbacks = 0
        try:
            self.properties = self.lib.obs_get_source_properties(source_id.encode())
            if self.properties == self.ffi.NULL:
                raise RuntimeError(f"Capture plugin properties unavailable: {source_id}")
            self.settings = self.lib.obs_get_source_defaults(source_id.encode())
            if self.settings == self.ffi.NULL:
                self.settings = self.lib.obs_data_create()
            if self.settings == self.ffi.NULL:
                raise RuntimeError("Unable to allocate camera metadata settings")
            self.lib.obs_properties_apply_settings(self.properties, self.settings)
        except BaseException:  # noqa: BLE001 - native allocation cleanup before propagating
            self.close()
            raise

    def __enter__(self) -> _NativeProperties:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.properties != self.ffi.NULL:
            self.lib.obs_properties_destroy(self.properties)
            self.properties = self.ffi.NULL
        if self.settings != self.ffi.NULL:
            self.lib.obs_data_release(self.settings)
            self.settings = self.ffi.NULL

    def _property(self, name: str):
        prop = self.lib.obs_properties_get(self.properties, name.encode())
        if prop == self.ffi.NULL:
            raise CameraPropertiesError("capability_property_missing")
        return prop

    def choices(self, name: str) -> tuple[tuple[str, str | int], ...]:
        prop = self._property(name)
        if self.lib.obs_property_get_type(prop) != 6:
            raise CameraPropertiesError("capability_property_type")
        count = self.lib.obs_property_list_item_count(prop)
        if count > _LIMIT:
            raise CameraPropertiesError("capability_overflow")
        kind = self.lib.obs_property_list_format(prop)
        if kind not in (1, 3):
            raise CameraPropertiesError("capability_property_type")
        result: list[tuple[str, str | int]] = []
        for index in range(count):
            if self.lib.obs_property_list_item_disabled(prop, index):
                continue
            label = self.lib.obs_property_list_item_name(prop, index)
            text = self.ffi.string(label).decode("utf-8", "replace") if label != self.ffi.NULL else ""
            if kind == 3:
                raw = self.lib.obs_property_list_item_string(prop, index)
                value = self.ffi.string(raw).decode("utf-8", "replace") if raw != self.ffi.NULL else ""
            else:
                value = int(self.lib.obs_property_list_item_int(prop, index))
            result.append((text, value))
        return tuple(result)

    def set(self, name: str, value: str | int | bool | Fraction) -> None:
        prop = self._property(name)
        key = name.encode()
        if isinstance(value, bool):
            self.lib.obs_data_set_bool(self.settings, key, value)
        elif isinstance(value, int):
            self.lib.obs_data_set_int(self.settings, key, value)
        elif isinstance(value, str):
            self.lib.obs_data_set_string(self.settings, key, value.encode())
        elif isinstance(value, Fraction):
            fps = self.ffi.new("struct media_frames_per_second *")
            fps.numerator, fps.denominator = value.numerator, value.denominator
            self.lib.obs_data_set_frames_per_second(self.settings, key, fps[0], self.ffi.NULL)
        else:
            raise TypeError("Unsupported camera metadata setting")
        self._callbacks += 1
        if self._callbacks > _CALLBACK_LIMIT:
            raise CameraPropertiesError("capability_overflow")
        self.lib.obs_property_modified(prop, self.settings)

    def fps_ranges(self) -> tuple[tuple[Fraction, Fraction], ...]:
        prop = self._property("frame_rate")
        if self.lib.obs_property_get_type(prop) != 11:
            raise CameraPropertiesError("capability_property_type")
        count = self.lib.obs_property_frame_rate_fps_ranges_count(prop)
        if count > _LIMIT:
            raise CameraPropertiesError("capability_overflow")
        result = []
        for index in range(count):
            lower = self.lib.obs_property_frame_rate_fps_range_min(prop, index)
            upper = self.lib.obs_property_frame_rate_fps_range_max(prop, index)
            low = Fraction(int(lower.numerator), int(lower.denominator))
            high = Fraction(int(upper.numerator), int(upper.denominator))
            if low <= 0 or high < low:
                raise CameraPropertiesError("invalid_frame_rate_range")
            result.append((low, high))
        return tuple(result)


def _dshow_media_type(value: int) -> CameraMediaType | None:
    if value == 400:
        return CameraMediaType.JPEG
    if value == 401:
        return CameraMediaType.H264
    return CameraMediaType.RAW if value in _DSHOW_RAW else None


def _dshow_modes(props: _NativeProperties, device_id: str, *, requested_size: tuple[int, int] | None = None):
    modes: dict[LocalVideoFormat, None] = {}
    props.set("video_device_id", device_id)
    props.set("res_type", 1)
    for _label, resolution in props.choices("resolution"):
        if not isinstance(resolution, str):
            continue
        parts = resolution.split("x")
        if len(parts) != 2:
            continue
        try:
            width, height = (int(part) for part in parts)
            LocalVideoFormat(CameraMediaType.RAW, "validation", width, height, 1, 1)
        except ValueError:
            continue
        if requested_size is not None and (width, height) != requested_size:
            continue
        # Reset both dependent settings before each resolution. The callbacks
        # otherwise retain the preceding format/FPS and hide valid alternatives.
        props.set("video_format", 0)
        props.set("frame_interval", 0)
        props.set("resolution", resolution)
        intervals = props.choices("frame_interval")
        for _fps_label, interval in intervals:
            if not isinstance(interval, int) or interval <= 0:
                continue  # "match output" and "highest" are policies, not modes.
            fps = _DSHOW_RATES.get(interval, Fraction(10_000_000, interval))
            if fps > 60:
                continue
            props.set("video_format", 0)
            props.set("frame_interval", interval)
            for _format_label, native_format in props.choices("video_format"):
                if not isinstance(native_format, int):
                    continue
                media_type = _dshow_media_type(native_format)
                if media_type is None:
                    continue
                mode = LocalVideoFormat(media_type, str(native_format), width, height,
                                        fps.numerator, fps.denominator)
                modes[mode] = None
                if len(modes) > _LIMIT:
                    raise CameraPropertiesError("capability_overflow")
    return tuple(modes), ()


def _avcapture_media_type(value: int) -> CameraMediaType | None:
    if value <= 0 or value > 0xFFFFFFFF:
        return None
    fourcc = value.to_bytes(4, "big")
    if not fourcc.isascii() or not all(32 <= char < 127 for char in fourcc):
        return None
    if fourcc in (b"hvc1", b"hev1"):
        return None
    if fourcc in (b"jpeg", b"mjpa", b"mjpb", b"MJPG"):
        return CameraMediaType.JPEG
    if fourcc in (b"avc1", b"H264"):
        return CameraMediaType.H264
    return CameraMediaType.RAW


def _avcapture_modes(props: _NativeProperties, device_id: str, *, requested_size: tuple[int, int] | None = None):
    modes: dict[LocalVideoFormat, None] = {}
    ranges: dict[LocalVideoFrameRateRange, None] = {}
    props.set("device", device_id)
    props.set("use_preset", False)
    for _label, resolution in props.choices("resolution"):
        if not isinstance(resolution, str):
            continue
        try:
            dimensions = json.loads(resolution)
            width, height = dimensions["width"], dimensions["height"]
            LocalVideoFormat(CameraMediaType.RAW, "validation", width, height, 1, 1)
        except (ValueError, TypeError, KeyError):
            continue
        if requested_size is not None and (width, height) != requested_size:
            continue
        props.set("resolution", resolution)
        native_ranges = props.fps_ranges()
        # OBS 32.1.2 enumerate_frame_rates() includes EVERY format's interval
        # endpoints; find_formats() uses inclusive continuous interval tests.
        # Thus format membership cannot change between adjacent endpoints.
        # Query each segment and its endpoints via the FPS modified callback;
        # never assign the aggregate range to every pixel format. This relies
        # specifically on that plugin contract, not a generic sampling heuristic.
        boundaries = sorted({fps for low, high in native_ranges for fps in (low, min(high, Fraction(60)))
                             if 0 < fps <= 60})
        candidates = set(boundaries)
        candidates.update(fps for fps in _USUAL_FPS if any(low <= fps <= high for low, high in native_ranges))
        candidates.update((low + high) / 2 for low, high in zip(boundaries, boundaries[1:], strict=False))
        supported: dict[Fraction, set[int]] = {}
        for fps in sorted(candidates):
            if not any(low <= fps <= high for low, high in native_ranges):
                continue
            props.set("frame_rate", fps)
            formats = {value for _label, value in props.choices("input_format")
                       if isinstance(value, int) and _avcapture_media_type(value) is not None}
            supported[fps] = formats
            for native_format in sorted(formats):
                media_type = _avcapture_media_type(native_format)
                assert media_type is not None
                mode = LocalVideoFormat(media_type, native_format.to_bytes(4, "big").decode("ascii"), width, height,
                                        fps.numerator, fps.denominator)
                modes[mode] = None
                if len(modes) > _LIMIT:
                    raise CameraPropertiesError("capability_overflow")
        for low, high in zip(boundaries, boundaries[1:], strict=False):
            common = supported.get(low, set()) & supported.get(high, set()) & supported.get((low + high) / 2, set())
            for native_format in sorted(common):
                media_type = _avcapture_media_type(native_format)
                assert media_type is not None
                minimum, maximum = 1 / high, 1 / low
                item = LocalVideoFrameRateRange(
                    media_type, native_format.to_bytes(4, "big").decode("ascii"), width, height,
                    LocalVideoFrameInterval(minimum.numerator, minimum.denominator),
                    LocalVideoFrameInterval(maximum.numerator, maximum.denominator),
                )
                ranges[item] = None
                if len(ranges) > _LIMIT:
                    raise CameraPropertiesError("capability_overflow")
    return tuple(modes), tuple(ranges)


def _backend():
    platform = sys.platform
    if platform == "win32":
        return "dshow_input", "video_device_id", "dshow", _dshow_modes
    elif platform == "darwin":
        return "av_capture_input", "device", "avcapture", _avcapture_modes
    else:
        raise RuntimeError("No OBS camera metadata backend for this platform")


def _inventory(source_id: str, device_key: str) -> tuple[tuple[str, str], ...]:
    from .obs_runtime import obs_runtime

    obs_runtime().ensure_started()
    with _NativeProperties(source_id) as inventory:
        choices = inventory.choices(device_key)
    seen = set()
    devices = []
    for name, device_id in choices:
        if not isinstance(device_id, str) or not device_id or device_id in seen:
            continue
        seen.add(device_id)
        if len(seen) > MAXIMUM_LOCAL_CAMERA_DEVICES:
            raise CameraPropertiesError("inventory_overflow")
        devices.append((name or device_id, device_id))
    return tuple(devices)


def _device_modes(source_id, backend, probe, name, device_id, *, requested_size=None) -> LocalCameraDevice:
    formats, ranges, error_code = (), (), ""
    try:
        with _NativeProperties(source_id) as properties:
            formats, ranges = probe(properties, device_id, requested_size=requested_size)
        if not formats and not ranges:
            error_code = "no_formats"
    except Exception as error:  # noqa: BLE001 - one driver must not hide inventory
        error_code = error.error_code if isinstance(error, CameraPropertiesError) else "capability_query_failed"
        log.warning("Camera metadata query failed (%s)", backend, exc_info=True)
    state = LocalCameraProbe(
        LocalCameraProbeStatus.UNVERIFIED if error_code else LocalCameraProbeStatus.READY,
        backend, "capabilities" if error_code else "", error_code,
    )
    return LocalCameraDevice(device_id, name, False, formats, state, frame_rate_ranges=ranges)


def discover_obs_cameras() -> list[LocalCameraDevice]:
    """Raise inventory failures; retain individual devices with capability errors."""
    source_id, device_key, backend, probe = _backend()
    return [_device_modes(source_id, backend, probe, name, device_id)
            for name, device_id in _inventory(source_id, device_key)]


def query_obs_camera_modes(query: LocalCameraModeQuery) -> LocalCameraDevice:
    """Run native callbacks only for an inventoried device's exact size."""
    source_id, device_key, backend, probe = _backend()
    for name, device_id in _inventory(source_id, device_key):
        if device_id == query.device_id:
            return _device_modes(source_id, backend, probe, name, device_id,
                                 requested_size=(query.width, query.height))
    raise OSError("Camera is no longer in the device inventory")
