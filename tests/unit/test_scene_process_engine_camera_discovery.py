from __future__ import annotations

from copy import deepcopy

import pytest

from solin.core.scenes.engine import LocalCameraProbeStatus
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, SceneIpcMessageError
from solin.core.scenes.process_engine import _local_camera_discovery_from_envelope


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
