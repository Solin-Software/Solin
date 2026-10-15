"""Camera discovery responsiveness and capture health on the sidecar boundary."""

from __future__ import annotations

import io
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from solin.core.media.obs_camera_capture import CameraCaptureState
from solin.core.scenes.engine import (
    LocalCameraDevice, LocalCameraProbe, LocalCameraProbeStatus, LocalVideoFormat,
    LocalVideoFrameInterval, LocalVideoFrameRateRange, LocalVideoResolutionRange,
)
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, encode_envelope, read_envelope
from solin.core.scenes.libobs_sidecar import (
    LibobsSidecarEngine, _CameraDiscoveryRequests, serve,
)
from solin.core.scenes.model import CameraMediaType, LocalCameraConfig
from solin.core.scenes.process_engine import (
    _local_camera_discovery_from_envelope, _source_health_from_envelope,
)


def _request(kind="list_local_cameras", identity="request-1", **changes):
    return replace(SceneIpcEnvelope(
        message_type=kind, request_id=identity, session_id="camera-test",
        process_generation="generation-1", sequence=1, document_revision=4,
        deadline_monotonic_ms=int(time.monotonic() * 1000) + 30_000, payload={},
    ), **changes)


def test_camera_discovery_reads_catalog_with_explicit_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr("solin.core.scenes.camera_catalog.camera_catalog", lambda: SimpleNamespace(
        devices=lambda **kwargs: calls.append(kwargs) or (),
    ))
    engine = LibobsSidecarEngine()
    result = engine._local_camera_list()
    assert calls == [{"refresh": True}]
    assert result["supported"] and result["ready"]
    assert result["devices"] == [] and result["error_code"] == ""


def test_serve_camera_discovery_does_not_block_ping_and_drains_before_shutdown():
    started, release, finished = (threading.Event() for _ in range(3))

    class Engine(LibobsSidecarEngine):
        def __init__(self):
            super().__init__()
            self._runtime_started = True

        def _local_camera_list(self):
            started.set()
            assert release.wait(3)
            finished.set()
            return {"devices": []}

        def shutdown(self):
            assert finished.is_set()

    discovery, ping = _request(), _request("ping", "ping")
    discovery_bytes = encode_envelope(discovery)

    class Source(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == len(discovery_bytes):
                assert started.wait(3)
            return super().read(size)

    class Sink(io.BytesIO):
        def write(self, data):
            response = read_envelope(io.BytesIO(data))
            assert response is not None
            if response.message_type == "heartbeat":
                assert started.is_set() and not finished.is_set()
                release.set()
            return super().write(data)

    try:
        sink = Sink()
        serve(Source(discovery_bytes + encode_envelope(ping)), sink, engine=Engine())
        assert finished.is_set()
    finally:
        release.set()


class _Replies:
    def __init__(self):
        self.items = []
        self.condition = threading.Condition()

    def emit(self, envelope):
        with self.condition:
            self.items.append(envelope)
            self.condition.notify_all()

    def wait(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.items) >= count, timeout=3)
            return tuple(self.items)


def test_concurrent_discovery_has_one_query_and_preserves_all_correlation():
    started, release = threading.Event(), threading.Event()
    calls, replies = [], _Replies()

    def discover():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {"generation": 7}

    worker = _CameraDiscoveryRequests(discover, replies.emit)
    requests = [_request(identity=f"camera-{index}", sequence=index) for index in range(3)]
    try:
        worker.submit(requests[0])
        assert started.wait(3)
        for request in requests[1:]:
            worker.submit(request)
        release.set()
        responses = replies.wait(3)
        assert calls == [1]
        for request, response in zip(requests, responses, strict=True):
            assert response == replace(request, message_type="local_camera_list", payload={"generation": 7})
    finally:
        release.set()
        worker.close()


def test_discovery_failure_allows_retry_and_expiration_is_per_request():
    replies, attempts = _Replies(), []

    def discover():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("device enumeration failed")
        return {"devices": []}

    worker = _CameraDiscoveryRequests(discover, replies.emit)
    try:
        worker.submit(_request())
        first = replies.wait(1)[0]
        assert first.payload["error_code"] == "camera_enumeration_failed"
        worker.submit(_request(identity="retry"))
        assert replies.wait(2)[1].payload == {"devices": []}
        worker.submit(_request(identity="expired", deadline_monotonic_ms=1))
        assert replies.wait(3)[2].payload["error_code"] == "deadline_exceeded"
        assert len(attempts) == 2
    finally:
        worker.close()


def test_invalid_discovery_request_does_not_enter_native_query():
    replies, calls = _Replies(), []
    worker = _CameraDiscoveryRequests(lambda: calls.append(1) or {}, replies.emit)
    try:
        worker.submit(_request(payload={"unexpected": True}))
        assert replies.wait(1)[0].payload["error_code"] == "invalid_request"
        assert calls == []
    finally:
        worker.close()


def test_size_queries_are_coalesced_without_mixing_inventory_or_other_sizes():
    started, release = threading.Event(), threading.Event()
    replies, calls = _Replies(), []

    def modes(query):
        calls.append((query.width, query.height))
        started.set()
        assert release.wait(3)
        return {"width": query.width, "height": query.height}

    worker = _CameraDiscoveryRequests(lambda: {"devices": []}, replies.emit, query_modes=modes)
    query = _request("query_local_camera_modes", "size-one", payload={
        "device_id": "camera", "width": 800, "height": 600,
    })
    try:
        worker.submit(query)
        assert started.wait(3)
        worker.submit(replace(query, request_id="same-size"))
        worker.submit(replace(query, request_id="other-size", payload={
            "device_id": "camera", "width": 1024, "height": 768,
        }))
        worker.submit(_request(identity="inventory"))
        release.set()
        results = {item.request_id: item for item in replies.wait(4)}
        assert calls == [(800, 600), (1024, 768)]
        assert results["size-one"].payload == results["same-size"].payload == {"width": 800, "height": 600}
        assert results["other-size"].payload == {"width": 1024, "height": 768}
        assert results["inventory"].message_type == "local_camera_list"
        assert all(results[key].message_type == "local_camera_modes" for key in ("size-one", "same-size", "other-size"))
    finally:
        release.set()
        worker.close()


def test_discovery_suppresses_stale_session_results():
    started, release, replies = threading.Event(), threading.Event(), _Replies()

    def discover():
        started.set()
        assert release.wait(3)
        return {"devices": []}

    worker = _CameraDiscoveryRequests(discover, replies.emit)
    try:
        worker.submit(_request())
        assert started.wait(3)
        worker.submit(_request(identity="new-session", process_generation="generation-2"))
        release.set()
        responses = replies.wait(2)
        assert responses[0].message_type == "error"
        assert responses[0].payload["error_code"] == "stale_request"
        assert responses[1].message_type == "local_camera_list"
    finally:
        release.set()
        worker.close()


def test_discovery_close_drains_native_work_and_suppresses_late_results():
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    replies = _Replies()

    def discover():
        started.set()
        assert release.wait(3)
        return {"devices": []}

    worker = _CameraDiscoveryRequests(discover, replies.emit)
    closing = threading.Thread(target=lambda: (worker.close(), closed.set()))
    try:
        worker.submit(_request())
        assert started.wait(3)
        closing.start()
        with worker._condition:
            assert worker._condition.wait_for(lambda: worker._closed, timeout=3)
        assert not closed.is_set()
        release.set()
        closing.join(3)
        assert closed.is_set()
        worker.submit(_request(identity="after-close"))
        assert replies.items == []
    finally:
        release.set()
        worker.close()
        if closing.ident is not None:
            closing.join(3)


def test_camera_capability_ranges_roundtrip_through_sidecar_and_client(monkeypatch):
    interval = LocalVideoFrameInterval
    mode = LocalVideoFormat(CameraMediaType.RAW, "YUY2", 1280, 720, 30000, 1001)
    device = LocalCameraDevice(
        "camera-1", "USB camera", False, (mode,),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "dshow"),
        (LocalVideoResolutionRange(CameraMediaType.RAW, "YUY2", 640, 1920, 16, 480, 1080, 2),),
        (LocalVideoFrameRateRange(CameraMediaType.RAW, "YUY2", 1280, 720,
                                 interval(1, 30), interval(1, 5), interval(1, 300)),),
    )
    monkeypatch.setattr("solin.core.scenes.camera_catalog.camera_catalog", lambda: SimpleNamespace(
        devices=lambda **kwargs: (device,),
    ))
    engine = LibobsSidecarEngine()
    engine._runtime_started = True
    response = engine.handle(_request())
    assert response is not None
    # Exercise JSON framing too: tuples and rational interval records cross the IPC boundary.
    wire = read_envelope(io.BytesIO(encode_envelope(response)))
    assert wire is not None
    assert _local_camera_discovery_from_envelope(wire).devices == (device,)


def test_enumeration_failure_is_distinct_from_empty_inventory(monkeypatch):
    def devices(**kwargs):
        raise OSError("camera plugin unavailable")

    monkeypatch.setattr("solin.core.scenes.camera_catalog.camera_catalog", lambda: SimpleNamespace(
        devices=devices,
    ))
    result = LibobsSidecarEngine()._local_camera_list()
    assert not result["supported"] and result["ready"]
    assert result["error_code"] == "camera_enumeration_failed"


def _health_engine():
    engine, events = LibobsSidecarEngine(), []
    runtime = SimpleNamespace(state=None)
    runtime.camera_capture_state = lambda device: runtime.state
    engine._runtime = cast(Any, runtime)
    engine._runtime_started = True
    engine._scene_graph = cast(Any, SimpleNamespace(
        media_only_source=None, content_source=None, hydrate=lambda *a, **kw: None,
    ))
    engine.set_event_sink(events.append)
    return engine, runtime, events


def _hydrate(engine, config=None, *, revision=4) -> SceneIpcEnvelope:
    config = config or LocalCameraConfig(device_id="camera-1")
    response = engine.handle(_request("hydrate", identity=f"hydrate-{revision}", document_revision=revision,
        payload={"document": {"sources": [{
            "id": "local-source", "type": "local_camera", "configuration": config.to_record(),
        }]}}))
    assert response is not None
    return response


def _ping(engine, **changes) -> SceneIpcEnvelope:
    response = engine.handle(_request("ping", **changes))
    assert response is not None
    return response


def test_hydrate_publishes_starting_until_capture_frames_are_observed():
    from solin.core.scenes.camera_catalog import camera_configuration_id

    engine, runtime, events = _health_engine()
    config = LocalCameraConfig(device_id="camera-1")
    assert _hydrate(engine, config).payload["applied"]
    first = _source_health_from_envelope(events[-1])
    assert first.status.value == "starting"
    assert first.configuration_id == camera_configuration_id(config)
    runtime.state = CameraCaptureState(1, "ready", width=1920, height=1080)
    assert _ping(engine).message_type == "heartbeat"
    ready = _source_health_from_envelope(events[-1])
    assert ready.status.value == "ready" and not ready.error_code
    assert events[-1].document_revision == 4
    assert len(events) == 2
    engine.handle(_request("ping"))
    assert len(events) == 2


@pytest.mark.parametrize("code", ["camera_no_frames", "camera_mode_mismatch", "camera_update_failed"])
def test_camera_failure_is_contextual_and_can_recover(code):
    engine, runtime, events = _health_engine()
    runtime.state = CameraCaptureState(3, "failed", code, width=640, height=480)
    assert _hydrate(engine).payload["applied"]
    failed = _source_health_from_envelope(events[-1])
    assert failed.status.value == "failed" and failed.error_code == code
    assert failed.message
    if code == "camera_mode_mismatch":
        assert "640×480" in failed.message
    runtime.state = CameraCaptureState(3, "ready", width=1920, height=1080)
    engine.handle(_request("ping"))
    assert _source_health_from_envelope(events[-1]).status.value == "ready"


def test_health_fences_old_native_generations_and_new_document_configuration():
    engine, runtime, events = _health_engine()
    runtime.state = CameraCaptureState(5, "ready", width=1920, height=1080)
    assert _hydrate(engine).payload["applied"]
    runtime.state = CameraCaptureState(4, "failed", "camera_no_frames")
    engine.handle(_request("ping"))
    assert len(events) == 1
    runtime.state = CameraCaptureState(6, "starting")
    manual = LocalCameraConfig("camera-1", 1280, 720, 30, 1, CameraMediaType.RAW, "YUY2")
    assert _hydrate(engine, manual, revision=5).payload["applied"]
    assert events[-1].document_revision == 5
    assert events[-1].payload["configuration_id"] != events[0].payload["configuration_id"]
    runtime.state = CameraCaptureState(7, "starting")
    assert _hydrate(engine, revision=6).payload["applied"]
    assert events[-1].document_revision == 6
    assert events[-1].payload["configuration_id"] == events[0].payload["configuration_id"]


def test_observation_error_after_ready_is_reported_once_and_does_not_block_ping():
    engine, runtime, events = _health_engine()
    runtime.state = CameraCaptureState(3, "ready", width=1920, height=1080)
    assert _hydrate(engine).payload["applied"]

    def failure(device):
        raise RuntimeError("capture observation unavailable")

    runtime.camera_capture_state = failure
    for _ in range(2):
        assert _ping(engine).message_type == "heartbeat"
    assert len(events) == 2
    assert events[-1].payload["error_code"] == "camera_observation_failed"


def test_old_session_cannot_publish_camera_health():
    engine, runtime, events = _health_engine()
    runtime.state = CameraCaptureState(1, "starting")
    assert _hydrate(engine).payload["applied"]
    runtime.state = CameraCaptureState(1, "ready", width=1920, height=1080)
    engine.handle(_request("ping", process_generation="generation-2"))
    assert len(events) == 1


def test_document_replacement_during_health_observation_suppresses_old_result():
    engine, runtime, events = _health_engine()
    runtime.state = CameraCaptureState(1, "starting")
    assert _hydrate(engine).payload["applied"]

    def superseded(device):
        engine._camera_health_request = None
        engine._camera_health_bindings = ()
        return CameraCaptureState(1, "ready", width=1920, height=1080)

    runtime.camera_capture_state = superseded
    engine.handle(_request("ping"))
    assert len(events) == 1


def test_shutdown_invalidates_catalog_and_stops_health_events(monkeypatch):
    calls = []
    monkeypatch.setattr("solin.core.scenes.camera_catalog.camera_catalog", lambda: SimpleNamespace(
        invalidate=lambda: calls.append("invalidate"),
    ))
    engine, runtime, events = _health_engine()
    runtime.shutdown = lambda: calls.append("shutdown")
    cast(Any, engine._scene_graph).shutdown = lambda: None
    assert _hydrate(engine).payload["applied"]
    engine.shutdown()
    engine.handle(_request("ping"))
    assert calls == ["invalidate", "shutdown"] and len(events) == 1
