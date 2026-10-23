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


def test_aiohttp_transport_reads_a_reply_split_across_segments() -> None:
    """Budget cameras send replies in several TCP writes; all of them count."""
    import socket
    import threading
    import time as _time

    from solin.core.scenes.ptz_onvif import AiohttpOnvifTransport

    body = _EMPTY_SOAP + b"<!--" + b"x" * 4096 + b"-->"
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(65536)
            headers, _, received = request.partition(b"\r\n\r\n")
            length = int(next(
                line.split(b":", 1)[1] for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ))
            while len(received) < length:
                received += connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body[:1000]
            )
            _time.sleep(0.2)
            connection.sendall(body[1000:])

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        response = AiohttpOnvifTransport().call(
            endpoint=f"http://127.0.0.1:{port}/onvif/device_service",
            action="urn:test",
            payload=b"<request/>",
            credentials=None,
            timeout_seconds=5,
            cancellation=PtzCancellation(),
        )
    finally:
        worker.join()
        server.close()

    assert response.status == 200
    assert response.body == body


def _set_preset_response(token: str) -> bytes:
    return f"""
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
          <s:Body><tptz:SetPresetResponse><tptz:PresetToken>{token}</tptz:PresetToken>
          </tptz:SetPresetResponse></s:Body>
        </s:Envelope>
    """.encode()


def test_onvif_creates_a_new_preset_and_returns_the_cameras_token() -> None:
    transport = _Transport([OnvifHttpResponse(200, _set_preset_response("17"))])
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)

    token = adapter.control(
        _control(PtzControlKind.CREATE_PRESET, preset_name="Pulpit"),
        PtzCancellation(),
    )

    assert token == "17"
    payload = ElementTree.fromstring(transport.calls[0].payload)
    assert payload.find(".//{*}PresetToken") is None  # the camera chooses it
    assert _element_text(transport.calls[0].payload, "PresetName") == "Pulpit"


def test_onvif_create_without_a_token_in_the_answer_fails() -> None:
    transport = _Transport([OnvifHttpResponse(200, _EMPTY_SOAP)])
    adapter = OnvifPtzAdapter(transport=transport, clock=lambda: 1.0)

    with pytest.raises(PtzExecutionError) as caught:
        adapter.control(
            _control(PtzControlKind.CREATE_PRESET, preset_name="Pulpit"),
            PtzCancellation(),
        )

    assert caught.value.error_code == "ptz_onvif_preset_token_missing"


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        (PtzControlKind.CREATE_PRESET, {}),
        (PtzControlKind.CREATE_PRESET, {"preset_name": "   "}),
        (PtzControlKind.STOP, {"preset_name": "Pulpit"}),
    ],
)
def test_preset_names_belong_only_to_create_requests(kind, changes) -> None:
    with pytest.raises(ValueError):
        _control(kind, **changes)
