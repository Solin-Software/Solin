"""DirectShow metadata, without constructing or activating capture sources.

Property lists are rebuilt by native modified callbacks. Never use pylibobs's
cached property snapshots for dependent modes. Native values are persisted
independently of translated labels and OBS's pixel enum.

The caller owns an initialized OBS metadata context and loaded camera plugin.
On Windows, COM must be initialized on the thread running these queries.
"""

from __future__ import annotations

import logging
import sys
from fractions import Fraction
from time import perf_counter
from typing import Any

from solin.core.scenes.engine import (
    MAXIMUM_LOCAL_CAMERA_CAPABILITIES, MAXIMUM_LOCAL_CAMERA_DEVICES,
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus,
    LocalVideoFormat,
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
    """A typed native camera inventory or capability failure."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class _NativeProperties:
    """Own raw properties and temporary settings with deterministic release."""

    def __init__(self, source_id: str) -> None:
        from pylibobs._ffi import ffi, get_lib

        self.ffi: Any = ffi
        self.lib: Any = get_lib()
        self.properties: Any = self.ffi.NULL
        self.settings: Any = self.ffi.NULL
        self._source_id = source_id.encode()
        self._callbacks = 0
        try:
            started = perf_counter()
            log.info("OBS camera inventory query started (%s)", source_id)
            try:
                self.properties = self.lib.obs_get_source_properties(self._source_id)
            finally:
                log.info("OBS camera inventory query finished (%s, elapsed_ms=%.1f)",
                         source_id, (perf_counter() - started) * 1000)
            if self.properties == self.ffi.NULL:
                raise RuntimeError(f"Capture plugin properties unavailable: {source_id}")
            self.reset()
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

    def reset(self) -> None:
        """Reset callback settings and budget for one device, keeping inventory.

        Fresh defaults discard native callback history and autoselected values,
        including DirectShow's last device/resolution and AVCapture's FPS.
        """
        if self.settings != self.ffi.NULL:
            self.lib.obs_data_release(self.settings)
            self.settings = self.ffi.NULL
        self._callbacks = 0
        self.settings = self.lib.obs_get_source_defaults(self._source_id)
        if self.settings == self.ffi.NULL:
            self.settings = self.lib.obs_data_create()
        if self.settings == self.ffi.NULL:
            raise RuntimeError("Unable to allocate camera metadata settings")
        self.lib.obs_properties_apply_settings(self.properties, self.settings)

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
        else:
            raise TypeError("Unsupported camera metadata setting")
        self._callbacks += 1
        if self._callbacks > _CALLBACK_LIMIT:
            raise CameraPropertiesError("capability_overflow")
        self.lib.obs_property_modified(prop, self.settings)



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


def _backend():
    platform = sys.platform
    if platform == "win32":
        return "dshow_input", "video_device_id", "dshow", _dshow_modes
    else:
        raise RuntimeError("No OBS camera metadata backend for this platform")


def _inventory(properties: _NativeProperties, device_key: str) -> tuple[tuple[str, str], ...]:
    choices = properties.choices(device_key)
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


def _device_modes(properties, backend, probe, name, device_id, *, requested_size=None) -> LocalCameraDevice:
    formats, ranges, error_code = (), (), ""
    try:
        properties.reset()
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
    """Query caller-initialized OBS; raise inventory errors and retain device errors."""
    source_id, device_key, backend, probe = _backend()
    with _NativeProperties(source_id) as properties:
        return [_device_modes(properties, backend, probe, name, device_id)
                for name, device_id in _inventory(properties, device_key)]


def query_obs_camera_modes(query: LocalCameraModeQuery) -> LocalCameraDevice:
    """Query caller-initialized OBS for an inventoried device's exact size."""
    source_id, device_key, backend, probe = _backend()
    with _NativeProperties(source_id) as properties:
        for name, device_id in _inventory(properties, device_key):
            if device_id == query.device_id:
                return _device_modes(properties, backend, probe, name, device_id,
                                     requested_size=(query.width, query.height))
    raise OSError("Camera is no longer in the device inventory")
