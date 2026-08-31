from __future__ import annotations

from copy import deepcopy
from concurrent.futures import Future
import json
from pathlib import Path
import time

import pytest

from solin.core.scenes.engine import LocalCameraProbeStatus
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, SceneIpcMessageError
from solin.core.scenes.process_engine import (
    SceneEngineProcessConfig,
    SceneEngineProtocolError,
    SubprocessSceneEngine,
    _PendingRequest,
    _local_camera_discovery_from_envelope,
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
            "pixel_format": "NV12",
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
