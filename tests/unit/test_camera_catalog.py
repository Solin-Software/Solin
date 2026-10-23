"""Camera mode policy and discovery cache, without native devices."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from solin.core.scenes.camera_catalog import (
    CameraCatalog,
    camera_configuration_id,
    select_automatic_mode,
)
from solin.core.scenes.engine import (
    LocalCameraDevice, LocalCameraModeQuery, LocalCameraProbe, LocalCameraProbeStatus, LocalVideoFormat,
    LocalVideoResolutionRange,
)
from solin.core.scenes.model import CameraMediaType, LocalCameraConfig, OnvifPtzBinding


def _mode(width=1920, height=1080, numerator=30, denominator=1, pixel="MJPG"):
    return LocalVideoFormat(CameraMediaType.JPEG, pixel, width, height, numerator, denominator)


def _device(*formats):
    return LocalCameraDevice("camera", "Camera", False, formats,
                             LocalCameraProbe(LocalCameraProbeStatus.READY, "test"))


def test_capture_fingerprint_ignores_ptz_and_tracks_capture_configuration():
    config = LocalCameraConfig(device_id="camera")
    fingerprint = camera_configuration_id(config)
    assert fingerprint == camera_configuration_id(
        replace(
            config,
            ptz_binding=OnvifPtzBinding(endpoint="http://camera.local/onvif"),
        )
    )
    assert fingerprint != camera_configuration_id(replace(config, device_id="other-camera"))


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
    catalog.devices()
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


def test_cached_automatic_selection_never_starts_native_discovery():
    calls = []
    catalog = CameraCatalog(lambda: calls.append(True) or [_device(_mode())])
    assert catalog.automatic_mode("camera") is None
    assert calls == []
    catalog.devices()
    assert catalog.automatic_mode("camera") == _mode()
    assert calls == [True]


@pytest.mark.parametrize("warm", [False, True])
def test_cached_automatic_does_not_wait_for_pending_discovery(warm):
    started, release = Event(), Event()
    calls = []

    def discover():
        calls.append(True)
        if not warm or len(calls) > 1:
            started.set()
            assert release.wait(5)
        return [_device(_mode())]

    catalog = CameraCatalog(discover)
    if warm:
        catalog.devices()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(catalog.devices, refresh=True)
        try:
            assert started.wait(5)
            selected = pool.submit(catalog.automatic_mode, "camera")
            assert selected.result(1) == (_mode() if warm else None)
        finally:
            release.set()
        assert pending.result(5) == (_device(_mode()),)


def test_cached_automatic_cannot_use_invalidated_inventory():
    catalog = CameraCatalog(lambda: [_device(_mode())])
    catalog.devices()
    catalog.invalidate()
    assert catalog.automatic_mode("camera") is None


def test_cancelled_dispatch_cannot_start_a_late_helper_until_runtime_reset(monkeypatch):
    created = []

    class Helper:
        def __init__(self):
            created.append(True)

        def discover(self):
            return []

    monkeypatch.setattr("solin.core.scenes.camera_discovery_process.CameraDiscoveryProcess", Helper)
    catalog = CameraCatalog()
    catalog.cancel_discovery()
    with pytest.raises(RuntimeError, match="cancelled"):
        catalog.devices()
    assert created == []
    catalog.invalidate()
    assert catalog.devices() == ()
    assert created == [True]


def test_cancelling_native_helper_releases_all_catalog_waiters(monkeypatch):
    started, cancelled, waiting = Event(), Event(), Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    class Helper:
        def discover(self):
            started.set()
            assert cancelled.wait(3)
            raise RuntimeError("cancelled native query")

        def close(self):
            cancelled.set()

    monkeypatch.setattr("solin.core.scenes.camera_catalog.Future", ObservedFuture)
    monkeypatch.setattr("solin.core.scenes.camera_discovery_process.CameraDiscoveryProcess", Helper)
    catalog = CameraCatalog()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog.devices)
        try:
            assert started.wait(3)
            second = pool.submit(catalog.devices)
            assert waiting.wait(3)
            catalog.cancel_discovery()
            for future in (first, second):
                with pytest.raises(RuntimeError, match="cancelled native query"):
                    future.result(3)
        finally:
            cancelled.set()
            catalog.cancel_discovery()
    assert catalog.automatic_mode("camera") is None


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


@pytest.mark.parametrize("change", ["invalidate", "refresh", "refresh_pending"])
def test_mode_query_rejects_inventory_changed_after_read(monkeypatch, change):
    read, resume, refreshing, finish_refresh = (Event() for _ in range(4))
    original = replace(_device(_mode(640, 480, numerator=5)), resolution_ranges=(
        LocalVideoResolutionRange(CameraMediaType.JPEG, "MJPG", 640, 1920, 16, 480, 1080, 8),
    ))
    replacement = replace(original, display_name="Replacement camera", resolution_ranges=())
    queried = _device(_mode(800, 600, numerator=15))
    inventories = iter(([original], [replacement]))
    queries = []

    def discover():
        devices = next(inventories)
        if change == "refresh_pending" and devices == [replacement]:
            refreshing.set()
            assert finish_refresh.wait(3)
        return devices

    catalog = CameraCatalog(discover, lambda query: queries.append(query) or queried)
    catalog.devices()
    devices = catalog.devices

    def paused_read():
        snapshot = devices()
        read.set()
        assert resume.wait(3)
        return snapshot

    monkeypatch.setattr(catalog, "devices", paused_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(catalog.modes, LocalCameraModeQuery("camera", 800, 600))
        refresh = None
        try:
            assert read.wait(3)
            if change == "invalidate":
                catalog.invalidate()
                assert devices() == (replacement,)
            elif change == "refresh":
                assert devices(refresh=True) == (replacement,)
            else:
                refresh = pool.submit(devices, refresh=True)
                assert refreshing.wait(3)
            resume.set()
            with pytest.raises(RuntimeError, match="inventory changed"):
                pending.result(3)
        finally:
            resume.set()
            finish_refresh.set()
        if refresh is not None:
            assert refresh.result(3) == (replacement,)

    assert queries == []
    assert devices() == (replacement,)
    assert catalog.automatic_mode("camera") == replacement.formats[0]


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


def test_mode_query_rejects_inventory_invalidated_during_discovery():
    started, release = Event(), Event()
    original = replace(_device(_mode()), resolution_ranges=(
        LocalVideoResolutionRange(CameraMediaType.JPEG, "MJPG", 640, 1920, 16, 480, 1080, 8),
    ))
    replacement = _device(_mode(1280, 720))
    discoveries, queries = [], []

    def discover():
        discoveries.append(True)
        if len(discoveries) == 1:
            started.set()
            assert release.wait(3)
            return [original]
        return [replacement]

    catalog = CameraCatalog(
        discover, lambda query: queries.append(query) or _device(_mode(800, 600)),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(catalog.modes, LocalCameraModeQuery("camera", 800, 600))
        try:
            assert started.wait(3)
            catalog.invalidate()
            assert catalog.devices() == (replacement,)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="inventory changed"):
            pending.result(3)

    assert discoveries == [True, True]
    assert queries == []
    assert catalog.devices() == (replacement,)
    assert catalog.automatic_mode("camera") == replacement.formats[0]


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


@pytest.mark.parametrize("platform,alias,matched", [
    ("win32", "Previous label:usb-device", True),
    ("linux", "camera", False),
    ("darwin", "camera", False),
])
def test_automatic_mode_resolves_physical_identity_without_changing_native_id(monkeypatch, platform, alias, matched):
    import sys

    monkeypatch.setattr(sys, "platform", platform)
    native_id = "Current label:USB-DEVICE" if platform == "win32" else "Camera"
    device = replace(_device(_mode()), device_id=native_id)
    catalog = CameraCatalog(lambda: [device])
    catalog.devices()
    assert catalog.automatic_mode(alias) == (_mode() if matched else None)
    assert catalog.devices()[0].device_id == native_id


def test_windows_custom_modes_use_inventory_selector_and_share_alias_cache(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    device = replace(_device(_mode()), device_id="Current label:USB-DEVICE", resolution_ranges=(
        LocalVideoResolutionRange(CameraMediaType.JPEG, "MJPG", 640, 1920, 16, 480, 1080, 8),
    ))
    queried = replace(device, formats=(_mode(800, 600),), resolution_ranges=())
    queries = []

    def query_modes(query):
        queries.append(query)
        return queried

    catalog = CameraCatalog(lambda: [device], query_modes)
    assert catalog.modes(LocalCameraModeQuery("Old label:usb-device", 800, 600)) == queried
    assert catalog.modes(LocalCameraModeQuery("USB-DEVICE", 800, 600)) == queried
    assert queries == [LocalCameraModeQuery(device.device_id, 800, 600)]
    assert _mode(800, 600) in catalog.devices()[0].formats


def test_automatic_preserves_native_clock_rate_slightly_above_thirty():
    from solin.core.scenes.camera_catalog import select_automatic_mode
    from solin.core.scenes.engine import LocalVideoFormat
    from solin.core.scenes.model import CameraMediaType
    mode = LocalVideoFormat(CameraMediaType.RAW, "420v", 1920, 1080, 3000003, 100000,
        native_format="native-format-identity")
    assert select_automatic_mode([mode]) is mode
