"""Single-flight camera capabilities and the shared automatic capture policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import Future
from fractions import Fraction
from hashlib import sha256
import json
from threading import Lock
from typing import TYPE_CHECKING

from dataclasses import replace

from solin.core.media.camera_identity import local_camera_device_key

from .engine import LocalCameraDevice, LocalCameraModeQuery, LocalVideoFormat
from .model import LocalCameraConfig

if TYPE_CHECKING:
    from .camera_discovery_process import CameraDiscoveryProcess


def camera_configuration_id(config: LocalCameraConfig) -> str:
    """Identify the capture request independently of scene geometry or PTZ edits."""
    record = config.to_record()
    record.pop("ptz_binding")
    return sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def select_automatic_mode(
    formats: Iterable[LocalVideoFormat], *, canvas_width: int = 1920, canvas_height: int = 1080,
) -> LocalVideoFormat | None:
    """Prefer fluid video, then detail, within the automatic 1080p/30 budget.

    30000/1001 and 30 belong to the same target tier. Stable ordering retains
    the backend's preference when its modes otherwise score identically.
    """
    eligible = [mode for mode in formats if
                mode.width <= 1920 and mode.height <= 1080
                and mode.fps_numerator <= 30 * mode.fps_denominator]
    ratio = Fraction(canvas_width, canvas_height)

    def score(mode: LocalVideoFormat):
        fps = Fraction(mode.fps_numerator, mode.fps_denominator)
        return (min(fps, Fraction(299, 10)), mode.width * mode.height,
                -abs(Fraction(mode.width, mode.height) - ratio))

    return max(eligible, key=score, default=None)


class CameraCatalog:
    """Cache native metadata only; never own or open a capture source.

    Native work runs in a disposable helper process, without the cache mutex.
    Capture reads the last completed snapshot and never waits for discovery.
    """

    def __init__(
        self, discover: Callable[[], list[LocalCameraDevice]] | None = None,
        query_modes: Callable[[LocalCameraModeQuery], LocalCameraDevice] | None = None,
    ) -> None:
        self._discover = discover or self._discover_native
        self._query_modes = query_modes or self._query_native
        self._lock = Lock()
        self._native: CameraDiscoveryProcess | None = None
        self._discovery_cancelled = False
        self._epoch = 0
        self._devices: tuple[LocalCameraDevice, ...] | None = None
        self._pending: Future[tuple[LocalCameraDevice, ...]] | None = None
        self._queries: dict[LocalCameraModeQuery, Future[LocalCameraDevice]] = {}

    def _native_process(self) -> CameraDiscoveryProcess:
        from .camera_discovery_process import CameraDiscoveryProcess

        with self._lock:
            if self._discovery_cancelled:
                raise RuntimeError("Camera discovery has been cancelled")
            if self._native is None:
                self._native = CameraDiscoveryProcess()
            return self._native

    def _discover_native(self) -> list[LocalCameraDevice]:
        return self._native_process().discover()

    def _query_native(self, query: LocalCameraModeQuery) -> LocalCameraDevice:
        return self._native_process().modes(query)

    def cancel_discovery(self) -> None:
        """Terminate the owned helper before joining its dispatch worker."""
        with self._lock:
            self._discovery_cancelled = True
            native, self._native = self._native, None
        if native is not None:
            native.close()

    def devices(self, *, refresh: bool = False) -> tuple[LocalCameraDevice, ...]:
        with self._lock:
            pending = self._pending
            if pending is None and self._devices is not None and not refresh:
                return self._devices
            owner = pending is None
            if owner:
                if refresh:
                    self._epoch += 1
                    self._queries.clear()
                pending = self._pending = Future()
            epoch = self._epoch
        assert pending is not None
        if not owner:
            return pending.result()
        try:
            devices = tuple(self._discover())
        except BaseException as error:  # noqa: BLE001 - release all single-flight waiters on failure
            with self._lock:
                if self._pending is pending:
                    self._devices = None
                    self._pending = None
            pending.set_exception(error)
            raise
        with self._lock:
            if self._pending is pending and self._epoch == epoch:
                self._devices = devices
                self._pending = None
        pending.set_result(devices)
        return devices

    def invalidate(self) -> None:
        """Drop metadata when its owning native runtime is stopped."""
        with self._lock:
            self._epoch += 1
            self._discovery_cancelled = False
            self._devices = None
            self._pending = None
            self._queries.clear()

    def modes(self, query: LocalCameraModeQuery) -> LocalCameraDevice:
        """Query a size's own intervals and cache them until inventory invalidation."""
        with self._lock:
            epoch = self._epoch
        devices = self.devices()
        with self._lock:
            if self._epoch != epoch:
                raise RuntimeError("Camera inventory changed during capability query")
            device = next((item for item in devices if local_camera_device_key(item.device_id) == local_camera_device_key(query.device_id)), None)
            if device is None or not any(item.supports(query.width, query.height)
                                         for item in device.resolution_ranges):
                raise ValueError("Camera resolution is outside its reported capabilities")
            # Query with the inventory's original native selector, even when an old
            # saved selector differs only in its friendly DirectShow name.
            query = replace(query, device_id=device.device_id)
            pending = self._queries.get(query)
            owner = pending is None
            if owner:
                pending = self._queries[query] = Future()
        assert pending is not None
        if not owner:
            return pending.result()
        try:
            result = self._query_modes(query)
            if (result.device_id != query.device_id or result.probe.error_code or result.resolution_ranges
                    or any((item.width, item.height) != (query.width, query.height)
                           for item in (*result.formats, *result.frame_rate_ranges))):
                raise ValueError("Camera capability query returned unrelated modes")
            with self._lock:
                if self._epoch == epoch and self._devices is not None:
                    self._devices = tuple(replace(
                        item, formats=tuple(dict.fromkeys((*item.formats, *result.formats))),
                        frame_rate_ranges=tuple(dict.fromkeys((*item.frame_rate_ranges, *result.frame_rate_ranges))),
                    ) if item.device_id == query.device_id else item for item in self._devices)
        except BaseException as error:  # noqa: BLE001 - release waiters and allow metadata retries
            with self._lock:
                if self._queries.get(query) is pending:
                    self._queries.pop(query)
            pending.set_exception(error)
            raise
        pending.set_result(result)
        return result

    def automatic_mode(
        self, device_id: str, *, canvas_width: int = 1920, canvas_height: int = 1080,
    ) -> LocalVideoFormat | None:
        """Select from completed metadata without starting or waiting on native work."""
        with self._lock:
            devices = self._devices or ()
        device = next((device for device in devices if local_camera_device_key(device.device_id) == local_camera_device_key(device_id)), None)
        if device is None:
            return None
        return select_automatic_mode(device.formats, canvas_width=canvas_width,
                                     canvas_height=canvas_height)


_CATALOG = CameraCatalog()


def camera_catalog() -> CameraCatalog:
    """Metadata shared by discovery and capture in the native process."""
    return _CATALOG
