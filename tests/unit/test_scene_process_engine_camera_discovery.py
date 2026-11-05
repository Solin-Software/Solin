from __future__ import annotations

from copy import deepcopy
from concurrent.futures import Future
from dataclasses import replace
import json
from pathlib import Path
import time
from unittest.mock import Mock

import pytest

from solin.core.scenes.engine import (
    EngineHealthEvent,
    LocalCameraModeQuery,
    LocalCameraProbeStatus,
    SceneEngineStatus,
)
from solin.core.scenes import process_engine
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, SceneIpcMessageError
from solin.core.scenes.process_engine import (
    SceneEngineCommandRejectedError,
    SceneEngineProcessConfig,
    SceneEngineProcessError,
    SceneEngineProtocolError,
    SceneEngineRequestTimeoutError,
    SubprocessSceneEngine,
    _PendingRequest,
    _local_camera_discovery_from_envelope,
    _local_camera_modes_from_envelope,
)


def _camera_payload() -> dict[str, object]:
    return {
        "supported": True,
        "ready": True,
        "generation": 4,
        "devices": [
            {
                "device_id": "camera://inventory-only",
                "display_name": "Inventory camera",
                "software_device": False,
                "formats": [],
                "resolution_ranges": [],
                "frame_rate_ranges": [],
                "probe": {
                    "status": "unverified",
                    "backend": "media_foundation",
                    "failure_stage": "capture_provider",
                    "error_code": "capture_provider_not_reported",
                    "native_error_code": "",
                },
            }
        ],
        "error_code": "",
    }


def _envelope(payload: dict[str, object]) -> SceneIpcEnvelope:
    return SceneIpcEnvelope(
        message_type="local_camera_list",
        request_id="camera-request",
        session_id="session-1",
        process_generation="generation-1",
        sequence=1,
        document_revision=0,
        deadline_monotonic_ms=1,
        payload=payload,
    )


def test_camera_discovery_parser_retains_structured_unverified_probe() -> None:
    discovery = _local_camera_discovery_from_envelope(_envelope(_camera_payload()))

    assert discovery.devices[0].probe.status is LocalCameraProbeStatus.UNVERIFIED
    assert discovery.devices[0].probe.failure_stage == "capture_provider"
    assert discovery.devices[0].probe.error_code == "capture_provider_not_reported"


@pytest.mark.parametrize("key", ["resolution_ranges", "frame_rate_ranges"])
@pytest.mark.parametrize("invalid", [None, {}, [None], [{}]])
def test_camera_discovery_parser_rejects_malformed_ranges(key, invalid):
    payload = _camera_payload()
    payload["devices"][0][key] = invalid
    with pytest.raises(SceneIpcMessageError):
        _local_camera_discovery_from_envelope(_envelope(payload))


def test_camera_discovery_requires_new_range_fields_in_versioned_contract():
    payload = _camera_payload()
    del payload["devices"][0]["resolution_ranges"]
    with pytest.raises(SceneIpcMessageError, match="fields"):
        _local_camera_discovery_from_envelope(_envelope(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "unknown"),
        ("backend", ""),
        ("failure_stage", ""),
        ("error_code", ""),
    ],
)
def test_camera_discovery_parser_rejects_invalid_probe_contract(
    field: str,
    value: str,
) -> None:
    payload = deepcopy(_camera_payload())
    devices = payload["devices"]
    assert isinstance(devices, list)
    device = devices[0]
    assert isinstance(device, dict)
    probe = device["probe"]
    assert isinstance(probe, dict)
    probe[field] = value

    with pytest.raises((SceneIpcMessageError, ValueError)):
        _local_camera_discovery_from_envelope(_envelope(payload))


def test_camera_discovery_parser_rejects_unknown_probe_fields() -> None:
    payload = deepcopy(_camera_payload())
    devices = payload["devices"]
    assert isinstance(devices, list)
    device = devices[0]
    assert isinstance(device, dict)
    probe = device["probe"]
    assert isinstance(probe, dict)
    probe["raw_debug_message"] = "must never cross the process boundary"

    with pytest.raises(SceneIpcMessageError, match="fields"):
        _local_camera_discovery_from_envelope(_envelope(payload))


def _invalid_format_payload() -> dict[str, object]:
    payload = _camera_payload()
    device = payload["devices"][0]
    device["device_id"] = "camera://private-device-serial"
    device["display_name"] = "Private camera name"
    device["probe"] = {
        "status": "ready",
        "backend": "media_foundation",
        "failure_stage": "",
        "error_code": "",
        "native_error_code": "",
    }
    device["formats"] = [
        {
            "media_type": "video/x-raw",
            "native_format": "", "pixel_format": "NV12",
            "width": 1920,
            "height": 1080,
            "fps_numerator": 30,
            "fps_denominator": 0,
        }
    ]
    return payload


def test_two_device_snapshot_can_grow_to_three_with_exact_driver_fps() -> None:
    payload = _invalid_format_payload()
    camera = payload["devices"][0]
    camera["formats"][0]["fps_denominator"] = 1
    camera["device_id"] = "camera://integrated"
    second = deepcopy(camera)
    second["device_id"] = "camera://virtual"
    second["software_device"] = True
    payload["devices"].append(second)
    first_discovery = _local_camera_discovery_from_envelope(_envelope(payload))
    assert len(first_discovery.devices) == 2

    third = deepcopy(camera)
    third["device_id"] = "camera://usb"
    third["formats"][0].update(fps_numerator=10_000_000, fps_denominator=333_333)
    payload["devices"].append(third)
    payload["generation"] = 5
    discovery = _local_camera_discovery_from_envelope(_envelope(payload))

    assert len(discovery.devices) == 3
    assert discovery.devices[:2] == first_discovery.devices
    assert discovery.devices[2].formats[0].fps_numerator == 10_000_000
    assert discovery.devices[2].formats[0].fps_denominator == 333_333


def test_camera_protocol_failure_logs_field_indices_and_preserves_cause(caplog) -> None:
    envelope = _envelope(_invalid_format_payload())
    engine = SubprocessSceneEngine(SceneEngineProcessConfig(executable=Path("unused")))
    engine._session_id = envelope.session_id
    engine._process_generation = envelope.process_generation
    future: Future[object] = Future()
    engine._pending[envelope.request_id] = _PendingRequest(
        expected_message_type=envelope.message_type,
        session_id=envelope.session_id,
        process_generation=envelope.process_generation,
        sequence=envelope.sequence,
        document_revision=envelope.document_revision,
        deadline_monotonic_ms=envelope.deadline_monotonic_ms,
        started_monotonic=time.monotonic(),
        is_liveness_probe=False,
        converter=_local_camera_discovery_from_envelope,
        future=future,
    )

    engine._receive(envelope, envelope.process_generation)

    with pytest.raises(SceneEngineProtocolError) as failure:
        future.result()
    assert failure.value.__cause__ is not None
    record = next(
        item.message for item in caplog.records if "scene_engine_protocol_error " in item.message
    )
    details = json.loads(record.split("scene_engine_protocol_error ", 1)[1])
    assert details["message_type"] == "local_camera_list"
    assert details["camera_generation"] == 4
    assert details["device_index"] == 0
    assert details["format_index"] == 0
    assert "FPS denominator" in details["reason"]
    assert details["format_values"]["fps_numerator"] == 30
    assert details["format_values"]["fps_denominator"] == 0
    assert "private-device-serial" not in caplog.text
    assert "Private camera name" not in caplog.text
    assert engine._generation_failed.is_set()


@pytest.mark.parametrize("level", ["list", "device", "format"])
def test_camera_validation_context_is_precise_and_does_not_echo_payload(level: str) -> None:
    payload = _invalid_format_payload()
    secret = "private-device-path-and-serial"
    expected_device = None
    expected_format = None
    if level == "list":
        payload[secret] = "unexpected field"
    elif level == "device":
        payload["devices"][0][secret] = "unexpected field"
        expected_device = 0
    else:
        payload["devices"][0]["formats"][0]["media_type"] = secret
        expected_device = 0
        expected_format = 0

    with pytest.raises(SceneIpcMessageError) as failure:
        _local_camera_discovery_from_envelope(_envelope(payload))

    diagnostics = failure.value.diagnostics
    assert diagnostics["device_index"] == expected_device
    assert diagnostics["format_index"] == expected_format
    assert secret not in str(failure.value)
    assert secret not in json.dumps(diagnostics)


def test_camera_diagnostics_do_not_echo_invalid_numeric_strings() -> None:
    payload = _invalid_format_payload()
    payload["devices"][0]["formats"][0]["fps_numerator"] = "private-device-serial"
    with pytest.raises(SceneIpcMessageError) as failure:
        _local_camera_discovery_from_envelope(_envelope(payload))
    diagnostics = failure.value.diagnostics
    assert diagnostics["format_values"]["fps_numerator"] == {"type": "str"}
    assert "private-device-serial" not in json.dumps(diagnostics)


def _mode_query():
    return LocalCameraModeQuery("camera://private-device-serial", 1920, 1080)


def _mode_query_payload():
    payload = _invalid_format_payload()
    payload["devices"][0]["formats"][0]["fps_denominator"] = 1
    return payload


def test_exact_size_query_parser_returns_only_the_requested_device_and_size():
    discovery = _local_camera_modes_from_envelope(_envelope(_mode_query_payload()), _mode_query())
    assert len(discovery.devices) == 1
    assert discovery.devices[0].formats[0].height == 1080


@pytest.mark.parametrize("mismatch", ["device", "size", "devices", "range"])
def test_exact_size_query_parser_rejects_cross_device_or_cross_resolution_data(mismatch):
    payload = _mode_query_payload()
    device = payload["devices"][0]
    if mismatch == "device":
        device["device_id"] = "camera://another"
    elif mismatch == "size":
        device["formats"][0]["width"] = 1280
    elif mismatch == "devices":
        extra = deepcopy(device)
        extra["device_id"] = "camera://another"
        payload["devices"].append(extra)
    else:
        device["frame_rate_ranges"] = [{
            "media_type": "video/x-raw", "native_format": "", "pixel_format": "NV12", "width": 640, "height": 480,
            "min_interval": {"numerator": 1, "denominator": 30},
            "max_interval": {"numerator": 1, "denominator": 5}, "step_interval": None,
        }]
    with pytest.raises(SceneIpcMessageError, match="Camera mode query"):
        _local_camera_modes_from_envelope(_envelope(payload), _mode_query())


def test_exact_size_query_parser_preserves_explicit_query_failure():
    payload = _camera_payload()
    payload["devices"] = []
    payload["error_code"] = "capability_query_failed"
    assert _local_camera_modes_from_envelope(_envelope(payload), _mode_query()).error_code == "capability_query_failed"
    payload["error_code"] = ""
    with pytest.raises(SceneIpcMessageError, match="no result"):
        _local_camera_modes_from_envelope(_envelope(payload), _mode_query())


def test_subprocess_exact_size_query_uses_typed_request_and_strict_converter(monkeypatch):
    engine = SubprocessSceneEngine(SceneEngineProcessConfig(executable=Path("unused")))
    requests = []
    pending = Future()
    def request(**fields):
        requests.append(fields)
        return pending
    monkeypatch.setattr(engine, "_request", request)
    assert engine.query_local_camera_modes(_mode_query().device_id, 1920, 1080,
                                           request_id="custom-size", deadline_ms=5000) is pending
    fields = requests[0]
    assert fields["message_type"] == "query_local_camera_modes"
    assert fields["expected_message_type"] == "local_camera_modes"
    assert fields["payload"] == {"device_id": _mode_query().device_id, "width": 1920, "height": 1080}
    assert fields["request_id"] == "custom-size"
    assert fields["deadline_ms"] == 5000
    assert fields["converter"](_envelope(_mode_query_payload())).devices[0].formats
    with pytest.raises(ValueError):
        engine.query_local_camera_modes(_mode_query().device_id, True, 1080,
                                        request_id="invalid", deadline_ms=5000)
    assert len(requests) == 1


@pytest.fixture(params=["list_local_cameras", "query_local_camera_modes"])
def metadata_type(request):
    return request.param


@pytest.fixture
def ready_process_engine(monkeypatch):
    clock = [10.0]
    engine = SubprocessSceneEngine(
        SceneEngineProcessConfig(executable=Path("unused"), maximum_restarts=0),
        monotonic=lambda: clock[0],
    )
    engine._started = engine._ready = True
    engine._session_id = "session-1"
    engine._process_generation = "generation-1"
    engine._last_received_monotonic = clock[0]
    engine._publish_health(SceneEngineStatus.READY, "")
    sent = []
    monkeypatch.setattr(engine, "_write", sent.append)
    yield engine, sent, clock
    engine._fail_all(SceneEngineProcessError("Test finished"))


def _request_metadata(engine, metadata_type, *, request_id="metadata", deadline_ms=20_000):
    if metadata_type == "list_local_cameras":
        return engine.list_local_cameras(request_id=request_id, deadline_ms=deadline_ms)
    query = _mode_query()
    return engine.query_local_camera_modes(
        query.device_id, query.width, query.height,
        request_id=request_id, deadline_ms=deadline_ms,
    )


def test_reconnection_dispatch_only_acknowledges_policy_admission(ready_process_engine):
    engine, sent, _clock = ready_process_engine
    future = engine.reconcile_local_cameras(7, request_id="topology", deadline_ms=5000)
    assert sent[-1].message_type == "reconcile_local_cameras"
    assert sent[-1].payload == {"topology_revision": 7}
    engine._receive(replace(sent[-1], message_type="ack", payload={
        "applied": True, "error_code": "", "error_message": "",
    }), sent[-1].process_generation)
    assert future.result().applied


@pytest.mark.parametrize("revision", [-1, True, "1", None])
def test_reconnection_dispatch_rejects_invalid_topology_revision(ready_process_engine, revision):
    engine, sent, _clock = ready_process_engine
    with pytest.raises(SceneIpcMessageError):
        engine.reconcile_local_cameras(revision, request_id="topology", deadline_ms=5000)
    assert not sent


def _metadata_response(request):
    modes = request.message_type == "query_local_camera_modes"
    return replace(
        request,
        message_type="local_camera_modes" if modes else "local_camera_list",
        payload=_mode_query_payload() if modes else _camera_payload(),
    )


def _request_foreground(engine, *, request_id="foreground", deadline_ms=30_000):
    return engine.set_window_targets(
        (), request_id=request_id, sequence=1, deadline_ms=deadline_ms,
    )


def test_camera_metadata_does_not_supply_command_liveness(ready_process_engine, metadata_type):
    engine, _sent, _clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)

    assert not future.done()
    assert engine._command_liveness_deadline() is None
    assert not engine._pending["metadata"].is_liveness_probe


def test_camera_metadata_expiry_preserves_foreground_liveness(ready_process_engine, metadata_type):
    engine, sent, clock = ready_process_engine
    metadata = _request_metadata(engine, metadata_type)
    clock[0] += 1
    foreground = _request_foreground(engine)
    deadline = sent[-1].deadline_monotonic_ms / 1000
    clock[0] += 20

    engine._expire_requests(clock[0])

    with pytest.raises(SceneEngineRequestTimeoutError):
        metadata.result()
    assert not foreground.done()
    assert engine.metrics.timeout_count == 1
    assert engine._last_command_timeout_monotonic is None
    assert engine._command_liveness_deadline() == deadline
    assert not engine._generation_failed.is_set()
    assert engine.health.status is SceneEngineStatus.READY


@pytest.mark.parametrize("foreground_first", [True, False])
def test_camera_metadata_does_not_extend_foreground_deadline(
    ready_process_engine, metadata_type, foreground_first,
):
    engine, sent, clock = ready_process_engine
    if not foreground_first:
        _request_metadata(engine, metadata_type)
        clock[0] += 1
    foreground = _request_foreground(engine, deadline_ms=5000)
    deadline = sent[-1].deadline_monotonic_ms / 1000
    if foreground_first:
        clock[0] += 1
        _request_metadata(engine, metadata_type)

    assert engine._command_liveness_deadline() == deadline
    clock[0] = deadline + 1
    engine._expire_requests(clock[0])
    with pytest.raises(SceneEngineRequestTimeoutError):
        foreground.result()
    assert engine._command_liveness_deadline() is None
    assert engine._last_command_timeout_monotonic == clock[0]

    # Expiring metadata later must preserve the original foreground timeout barrier.
    foreground_timeout = clock[0]
    clock[0] += 20
    engine._expire_requests(clock[0])
    assert engine._last_command_timeout_monotonic == foreground_timeout


def test_heartbeat_expiry_detects_engine_failure_with_metadata_pending(
    ready_process_engine, metadata_type,
):
    engine, _sent, clock = ready_process_engine
    metadata = _request_metadata(engine, metadata_type)
    engine._send_heartbeat()
    clock[0] += 3

    engine._expire_requests(clock[0])

    assert not metadata.done()
    assert engine._generation_failed.is_set()
    assert engine._heartbeat_request_id is None
    assert engine._last_command_timeout_monotonic is None


@pytest.mark.parametrize("responsive", [True, False])
def test_supervisor_checks_engine_health_while_camera_metadata_hangs(
    ready_process_engine, metadata_type, responsive, monkeypatch,
):
    engine, sent, clock = ready_process_engine
    events = []
    engine.subscribe(events.append)
    metadata = []
    started = clock[0]

    def write(request):
        sent.append(request)
        if request.message_type == "hello":
            response = replace(request, message_type="hello_ack", payload={
                "local_cameras": True, "rtsp_cameras": False,
                "hardware_compositing": False, "virtual_camera": False,
                "program_recording": False, "audio_input_capture": False,
                "system_audio_capture": False,
            })
        elif request.message_type == "ping" and responsive:
            response = replace(request, message_type="heartbeat", payload={"monotonic_ms": 1})
        else:
            return
        engine._receive(response, response.process_generation)

    def tick(_timeout):
        clock[0] += 0.5
        if clock[0] >= started + 6:
            engine._stop_event.set()
            return True
        if not metadata:
            metadata.append(_request_metadata(engine, metadata_type))
        return False

    # Drive the real supervisor loop synchronously; there are no pipe readers in this unit test.
    monkeypatch.setattr(process_engine.threading, "Thread", Mock())
    monkeypatch.setattr(engine, "_launch_process", lambda _generation: Mock(poll=lambda: None))
    monkeypatch.setattr(engine, "_terminate_process", Mock())
    monkeypatch.setattr(engine, "_stop_process_gracefully", Mock())
    monkeypatch.setattr(engine, "_write", write)
    monkeypatch.setattr(engine._stop_event, "wait", tick)

    engine._supervise(engine._config.hello_timeout_ms)

    statuses = [event.health.status for event in events if isinstance(event, EngineHealthEvent)]
    assert any(request.message_type == "ping" for request in sent)
    assert engine.metrics.timeout_count == 0
    assert engine.health.restart_count == 0
    if responsive:
        assert sum(request.message_type == "ping" for request in sent) >= 5
        assert SceneEngineStatus.FAILED not in statuses
        assert not metadata[0].done()
    else:
        assert engine.health.status is SceneEngineStatus.FAILED
        assert clock[0] - started <= engine._config.heartbeat_timeout_ms / 1000 + 0.5
        with pytest.raises(SceneEngineProcessError, match="generation ended"):
            metadata[0].result()


def test_foreground_commands_keep_oldest_deadline_and_timeout_barrier(ready_process_engine):
    engine, sent, clock = ready_process_engine
    first = _request_foreground(engine, request_id="first", deadline_ms=5000)
    oldest_deadline = sent[-1].deadline_monotonic_ms / 1000
    clock[0] += 1
    second = _request_foreground(engine, request_id="second", deadline_ms=30_000)
    engine._send_heartbeat()
    clock[0] += 3
    engine._expire_requests(clock[0])
    assert engine._command_liveness_deadline() == oldest_deadline
    assert not engine._generation_failed.is_set()

    clock[0] += 2
    engine._expire_requests(clock[0])
    with pytest.raises(SceneEngineRequestTimeoutError):
        first.result()
    assert not second.done()
    assert engine._last_command_timeout_monotonic == clock[0]
    assert engine._command_liveness_deadline() is None


def test_camera_metadata_response_still_completes_typed_request(ready_process_engine, metadata_type):
    engine, sent, _clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)
    response = _metadata_response(sent[-1])

    engine._receive(response, response.process_generation)

    assert future.result().devices[0].device_id == response.payload["devices"][0]["device_id"]
    assert engine.metrics.latency_sample_count == 1
    assert not engine._pending
    assert not engine._generation_failed.is_set()


def test_camera_metadata_rejection_does_not_fail_engine(ready_process_engine, metadata_type):
    engine, sent, _clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)
    response = replace(sent[-1], message_type="error", payload={
        "error_code": "capability_query_failed", "error_message": "Camera query failed",
    })

    engine._receive(response, response.process_generation)

    with pytest.raises(SceneEngineCommandRejectedError) as failure:
        future.result()
    assert failure.value.error_code == "capability_query_failed"
    assert engine.metrics.rejected_count == 1
    assert engine.health.status is SceneEngineStatus.READY
    assert not engine._generation_failed.is_set()


@pytest.mark.parametrize("field,value", [
    ("session_id", "stale-session"), ("process_generation", "stale-generation"),
    ("request_id", "unrelated-request"),
])
def test_camera_metadata_ignores_unrelated_responses(
    ready_process_engine, metadata_type, field, value,
):
    engine, sent, _clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)
    response = _metadata_response(sent[-1])
    engine._receive(replace(response, **{field: value}), response.process_generation)
    engine._receive(response, "stale-reader-generation")
    assert not future.done()
    assert "metadata" in engine._pending
    assert engine.metrics.protocol_error_count == 0

    engine._receive(response, response.process_generation)
    assert future.result().ready


@pytest.mark.parametrize("field,value", [
    ("message_type", "ack"), ("sequence", 1), ("document_revision", 1),
    ("deadline_monotonic_ms", 1), ("payload", {}),
])
def test_camera_metadata_rejects_mismatching_responses(
    ready_process_engine, metadata_type, field, value,
):
    engine, sent, _clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)
    response = replace(_metadata_response(sent[-1]), **{field: value})

    engine._receive(response, response.process_generation)

    with pytest.raises(SceneEngineProtocolError):
        future.result()
    assert engine.metrics.protocol_error_count == 1
    assert engine._generation_failed.is_set()


@pytest.mark.parametrize("cancelled", [True, False])
def test_camera_metadata_late_response_does_not_revive_request(
    ready_process_engine, metadata_type, cancelled,
):
    engine, sent, clock = ready_process_engine
    future = _request_metadata(engine, metadata_type)
    response = _metadata_response(sent[-1])
    if cancelled:
        assert future.cancel()
    else:
        clock[0] += 21
        engine._expire_requests(clock[0])

    engine._receive(response, response.process_generation)

    if cancelled:
        assert future.cancelled()
    else:
        with pytest.raises(SceneEngineRequestTimeoutError):
            future.result()
    assert not engine._pending
    assert engine.metrics.latency_sample_count == 0
    assert engine.metrics.protocol_error_count == 0
    assert not engine._generation_failed.is_set()
