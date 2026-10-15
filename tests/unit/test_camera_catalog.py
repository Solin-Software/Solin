"""Camera mode policy and discovery cache, without native devices."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from solin.core.scenes.camera_catalog import CameraCatalog, select_automatic_mode
from solin.core.scenes.engine import (
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus, LocalVideoFormat,
    LocalVideoResolutionRange,
)
from solin.core.scenes.model import CameraMediaType


def _mode(width=1920, height=1080, numerator=30, denominator=1, pixel="MJPG"):
    return LocalVideoFormat(CameraMediaType.JPEG, pixel, width, height, numerator, denominator)


def _device(*formats):
    return LocalCameraDevice("camera", "Camera", False, formats,
                             LocalCameraProbe(LocalCameraProbeStatus.READY, "test"))


def test_automatic_prefers_fluid_hd_over_slow_full_hd():
    assert select_automatic_mode((_mode(numerator=5), _mode(1280, 720))) == _mode(1280, 720)


def test_automatic_treats_ntsc_as_target_rate_and_honors_budget():
    selected = _mode(numerator=30000, denominator=1001)
    assert select_automatic_mode((_mode(1280, 720), selected, _mode(3840, 2160),
                                  _mode(numerator=60))) == selected


def test_automatic_retains_backend_preference_for_equivalent_formats():
    assert select_automatic_mode((_mode(pixel="first"), _mode(pixel="second"))) == _mode(pixel="first")
    assert select_automatic_mode((_mode(3840, 2160),)) is None
    assert select_automatic_mode(()) is None


def test_catalog_reuses_capabilities_and_refresh_replaces_inventory():
    calls = []

    def discover():
        calls.append(True)
        return [_device(_mode())] if len(calls) == 1 else []

    catalog = CameraCatalog(discover)
    assert catalog.automatic_mode("camera") == _mode()
    assert catalog.devices() == (_device(_mode()),)
    assert len(calls) == 1
    assert catalog.devices(refresh=True) == ()
    assert catalog.automatic_mode("camera") is None
    assert len(calls) == 2


def test_catalog_coalesces_concurrent_requests_without_holding_native_lock(monkeypatch):
    started, release, waiting = Event(), Event(), Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    monkeypatch.setattr("solin.core.scenes.camera_catalog.Future", ObservedFuture)

    def discover():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return [_device(_mode())]

    catalog = CameraCatalog(discover)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog.devices)
        assert started.wait(5)
        second = pool.submit(catalog.devices, refresh=True)
        assert waiting.wait(5)
        release.set()
        assert first.result(5) == second.result(5)
    assert len(calls) == 1


def test_catalog_does_not_turn_failure_into_empty_inventory_or_cache_failure():
    calls = []

    def discover():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("plugin unavailable")
        return []

    catalog = CameraCatalog(discover)
    with pytest.raises(RuntimeError, match="plugin unavailable"):
        catalog.devices()
    assert catalog.devices() == ()


def test_custom_size_queries_own_rates_and_caches_until_refresh():
    device = replace(_device(_mode()), resolution_ranges=(LocalVideoResolutionRange(
        CameraMediaType.JPEG, "MJPG", 640, 1920, 16, 480, 1080, 8,
    ),))
    queried = _device(_mode(800, 600, numerator=15))
    calls = []
    catalog = CameraCatalog(lambda: [device], lambda query: calls.append(query) or queried)
    query = LocalCameraModeQuery("camera", 800, 600)
    assert catalog.modes(query) == queried
    assert catalog.modes(query) == queried
    assert calls == [query]
    assert catalog.devices()[0].formats == (*device.formats, *queried.formats)
    with pytest.raises(ValueError, match="outside"):
        catalog.modes(LocalCameraModeQuery("camera", 801, 600))
    catalog.devices(refresh=True)
    assert catalog.modes(query) == queried
    assert calls == [query, query]


def test_invalidation_during_native_query_cannot_restore_stale_inventory():
    started, release = Event(), Event()
    calls = []

    def discover():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            return [_device(_mode())]
        return []

    catalog = CameraCatalog(discover)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(catalog.devices)
        assert started.wait(5)
        catalog.invalidate()
        assert catalog.devices() == ()
        release.set()
        assert old.result(5) == (_device(_mode()),)
    assert catalog.devices() == ()


def test_failed_custom_size_query_can_retry_and_does_not_merge_other_sizes():
    device = replace(_device(_mode()), resolution_ranges=(LocalVideoResolutionRange(
        CameraMediaType.JPEG, "MJPG", 640, 1920, 16, 480, 1080, 8,
    ),))
    choices = iter([device, _device(_mode(800, 600))])
    catalog = CameraCatalog(lambda: [device], lambda query: next(choices))
    query = LocalCameraModeQuery("camera", 800, 600)
    with pytest.raises(ValueError, match="unrelated"):
        catalog.modes(query)
    assert catalog.devices() == (device,)
    assert catalog.modes(query).formats == (_mode(800, 600),)
