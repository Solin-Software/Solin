from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from xml.etree import ElementTree

import pytest

from solin.core.scenes.model import CameraPreset, OnvifPtzBinding, PtzPosition
from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzControlKind,
    PtzControlRequest,
    PtzCredentials,
    PtzExecutionError,
    PtzRecallRequest,
)
from solin.core.scenes.ptz_onvif import (
    OnvifHttpResponse,
    OnvifPtzAdapter,
)


_EMPTY_SOAP = b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body /></s:Envelope>'


def _status(pan_tilt: str, zoom: str = "IDLE") -> bytes:
    return f"""
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
                    xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Body><tptz:GetStatusResponse><tptz:PTZStatus>
            <tt:MoveStatus><tt:PanTilt>{pan_tilt}</tt:PanTilt><tt:Zoom>{zoom}</tt:Zoom></tt:MoveStatus>
          </tptz:PTZStatus></tptz:GetStatusResponse></s:Body>
        </s:Envelope>
    """.encode()


@dataclass
class _Call:
    endpoint: str
    action: str
    payload: bytes
    credentials: PtzCredentials | None
    timeout_seconds: float


class _Transport:
    def __init__(self, responses: list[OnvifHttpResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[_Call] = []

    def call(
        self,
        *,
        endpoint: str,
        action: str,
        payload: bytes,
        credentials: PtzCredentials | None,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> OnvifHttpResponse:
        assert not cancellation.cancelled
        self.calls.append(_Call(endpoint, action, payload, credentials, timeout_seconds))
        return self.responses.popleft()


class _Credentials:
    def resolve(self, credential_ref: str) -> PtzCredentials | None:
        assert credential_ref == "credential-one"
        return PtzCredentials("operator", "not-persisted-password")


def _request(preset: CameraPreset, *, credential_ref: str = "") -> PtzRecallRequest:
    return PtzRecallRequest(
        request_id="request-one",
        camera_source_id="camera-one",
        binding=OnvifPtzBinding(
            endpoint="https://camera.test/onvif/ptz",
            profile_token="profile-one",
            credential_ref=credential_ref,
        ),
        preset=preset,
        deadline_monotonic=10_000.0,
    )


def _control(kind: PtzControlKind, **changes) -> PtzControlRequest:
    values = {
        "request_id": "control-one",
        "camera_source_id": "camera-one",
        "binding": OnvifPtzBinding(
            endpoint="https://camera.test/onvif/ptz",
            profile_token="profile-one",
        ),
        "kind": kind,
        "deadline_monotonic": 10_000.0,
    }
    values.update(changes)
    return PtzControlRequest(**values)


def _element_text(payload: bytes, name: str) -> str:
    root = ElementTree.fromstring(payload)
    element = root.find(f".//{{*}}{name}")
    assert element is not None
    return element.text or ""


def test_onvif_recall_sends_goto_preset_and_waits_for_stable_idle() -> None:
    transport = _Transport(
        [
            OnvifHttpResponse(200, _EMPTY_SOAP),
            OnvifHttpResponse(200, _status("MOVING")),
            OnvifHttpResponse(200, _status("IDLE")),
            OnvifHttpResponse(200, _status("IDLE")),
        ]
    )
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Lectern",
        remote_token="remote-one",
    )

    adapter.recall(_request(preset), PtzCancellation())

    assert transport.calls[0].action.endswith("/GotoPreset")
    assert _element_text(transport.calls[0].payload, "ProfileToken") == "profile-one"
    assert _element_text(transport.calls[0].payload, "PresetToken") == "remote-one"
    assert [call.action.rsplit("/", 1)[-1] for call in transport.calls] == [
        "GotoPreset",
        "GetStatus",
        "GetStatus",
        "GetStatus",
    ]


def test_onvif_absolute_recall_uses_normalized_position() -> None:
    transport = _Transport(
        [
            OnvifHttpResponse(200, _EMPTY_SOAP),
            OnvifHttpResponse(200, _status("IDLE")),
            OnvifHttpResponse(200, _status("IDLE")),
        ]
    )
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Wide",
        position=PtzPosition(pan=-0.25, tilt=0.5, zoom=0.75),
    )

    adapter.recall(_request(preset), PtzCancellation())

    root = ElementTree.fromstring(transport.calls[0].payload)
    pan_tilt = root.find(".//{*}PanTilt")
    zoom = root.find(".//{*}Zoom")
    assert transport.calls[0].action.endswith("/AbsoluteMove")
    assert pan_tilt is not None and pan_tilt.attrib == {"x": "-0.25", "y": "0.5"}
    assert zoom is not None and zoom.attrib == {"x": "0.75"}


def test_onvif_credentials_use_digest_token_without_plaintext_password() -> None:
    transport = _Transport(
        [
            OnvifHttpResponse(200, _EMPTY_SOAP),
            OnvifHttpResponse(200, _status("IDLE")),
            OnvifHttpResponse(200, _status("IDLE")),
        ]
    )
    adapter = OnvifPtzAdapter(
        transport=transport,
        credentials=_Credentials(),
        clock=lambda: 1.0,
    )
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Lectern",
        remote_token="1",
    )

    adapter.recall(_request(preset, credential_ref="credential-one"), PtzCancellation())

    payload = transport.calls[0].payload
    assert b"operator" in payload
    assert b"not-persisted-password" not in payload
    assert transport.calls[0].credentials == PtzCredentials(
        "operator", "not-persisted-password"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "ptz_authentication_failed"), (403, "ptz_authorization_failed")],
)
def test_onvif_maps_authentication_failures(status: int, expected: str) -> None:
    transport = _Transport([OnvifHttpResponse(status, _EMPTY_SOAP)])
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Lectern",
        remote_token="1",
    )

    with pytest.raises(PtzExecutionError, match=expected):
        adapter.recall(_request(preset), PtzCancellation())


def test_onvif_requires_move_status_for_completion_sensitive_take() -> None:
    transport = _Transport(
        [OnvifHttpResponse(200, _EMPTY_SOAP), OnvifHttpResponse(200, _EMPTY_SOAP)]
    )
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Lectern",
        remote_token="1",
    )

    with pytest.raises(PtzExecutionError, match="ptz_move_status_unavailable"):
        adapter.recall(_request(preset), PtzCancellation())


def test_onvif_controls_continuous_move_stop_and_preset_storage() -> None:
    transport = _Transport([OnvifHttpResponse(200, _EMPTY_SOAP) for _ in range(3)])
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)
    preset = CameraPreset(
        id="preset-one",
        camera_source_id="camera-one",
        name="Lectern",
        remote_token="remote-one",
    )

    adapter.control(
        _control(PtzControlKind.MOVE, pan=-0.4, tilt=0.2),
        PtzCancellation(),
    )
    adapter.control(_control(PtzControlKind.STOP), PtzCancellation())
    adapter.control(
        _control(PtzControlKind.STORE_PRESET, preset=preset),
        PtzCancellation(),
    )

    assert [call.action.rsplit("/", 1)[-1] for call in transport.calls] == [
        "ContinuousMove",
        "Stop",
        "SetPreset",
    ]
    move = ElementTree.fromstring(transport.calls[0].payload)
    pan_tilt = move.find(".//{*}PanTilt")
    assert pan_tilt is not None
    assert pan_tilt.attrib == {"x": "-0.4", "y": "0.2"}
    assert _element_text(transport.calls[2].payload, "PresetToken") == "remote-one"
