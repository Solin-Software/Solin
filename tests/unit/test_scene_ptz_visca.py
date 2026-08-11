from __future__ import annotations

from dataclasses import dataclass
import struct

import pytest

from solin.core.scenes.model import (
    CameraPreset,
    PtzPosition,
    PtzProtocol,
    ViscaIpPtzBinding,
    ViscaSerialPtzBinding,
    ViscaTransport,
)
from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzControlKind,
    PtzControlRequest,
    PtzExecutionError,
    PtzRecallRequest,
)
from solin.core.scenes.ptz_visca import (
    ViscaIpPtzAdapter,
    ViscaSerialPtzAdapter,
    _consume_reply,
    _ip_packet,
    _parse_ip_reply,
    _parse_visca_reply,
)


@dataclass
class _IpCall:
    host: str
    port: int
    transport: ViscaTransport
    sequence: int
    command: bytes
    timeout_seconds: float


class _IpTransport:
    def __init__(self) -> None:
        self.calls: list[_IpCall] = []

    def transact(
        self,
        *,
        host: str,
        port: int,
        transport: ViscaTransport,
        sequence: int,
        command: bytes,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> None:
        assert not cancellation.cancelled
        self.calls.append(
            _IpCall(host, port, transport, sequence, command, timeout_seconds)
        )


@dataclass
class _SerialCall:
    device_id: str
    baud_rate: int
    response_address: int
    command: bytes


class _SerialTransport:
    def __init__(self) -> None:
        self.calls: list[_SerialCall] = []

    def transact(
        self,
        *,
        device_id: str,
        baud_rate: int,
        response_address: int,
        command: bytes,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> None:
        assert timeout_seconds == pytest.approx(4.0)
        assert not cancellation.cancelled
        self.calls.append(
            _SerialCall(device_id, baud_rate, response_address, command)
        )


def _request(binding, *, remote_token: str = "7", position=None) -> PtzRecallRequest:
    return PtzRecallRequest(
        request_id="recall-1",
        camera_source_id="camera-1",
        binding=binding,
        preset=CameraPreset(
            id="preset-1",
            camera_source_id="camera-1",
            name="Wide",
            remote_token=remote_token,
            position=position,
            created_at="2026-08-02T12:00:00+00:00",
            updated_at="2026-08-02T12:00:00+00:00",
        ),
        deadline_monotonic=14.0,
    )


def _control(binding, kind: PtzControlKind, **changes) -> PtzControlRequest:
    values = {
        "request_id": "control-1",
        "camera_source_id": "camera-1",
        "binding": binding,
        "kind": kind,
        "deadline_monotonic": 14.0,
    }
    values.update(changes)
    return PtzControlRequest(**values)


def test_visca_ip_adapter_builds_numbered_preset_recall_and_sequences_calls() -> None:
    transport = _IpTransport()
    adapter = ViscaIpPtzAdapter(transport=transport, clock=lambda: 10.0)
    binding = ViscaIpPtzBinding(
        host="camera.local",
        port=52381,
        transport=ViscaTransport.UDP,
    )

    adapter.recall(_request(binding, remote_token="0x07"), PtzCancellation())
    adapter.recall(_request(binding, remote_token="8"), PtzCancellation())

    assert transport.calls == [
        _IpCall(
            "camera.local",
            52381,
            ViscaTransport.UDP,
            0,
            bytes.fromhex("81 01 04 3f 02 07 ff"),
            4.0,
        ),
        _IpCall(
            "camera.local",
            52381,
            ViscaTransport.UDP,
            1,
            bytes.fromhex("81 01 04 3f 02 08 ff"),
            4.0,
        ),
    ]
    assert adapter.protocol is PtzProtocol.VISCA_IP


def test_visca_serial_adapter_addresses_camera_and_response() -> None:
    transport = _SerialTransport()
    adapter = ViscaSerialPtzAdapter(transport=transport, clock=lambda: 10.0)
    binding = ViscaSerialPtzBinding(
        device_id="COM4",
        baud_rate=38400,
        camera_address=3,
    )

    adapter.recall(_request(binding, remote_token="12"), PtzCancellation())

    assert transport.calls == [
        _SerialCall(
            "COM4",
            38400,
            0x8B,
            bytes.fromhex("83 01 04 3f 02 0c ff"),
        )
    ]
    assert adapter.protocol is PtzProtocol.VISCA_SERIAL


def test_visca_ip_controls_pan_tilt_zoom_stop_and_preset_storage() -> None:
    transport = _IpTransport()
    adapter = ViscaIpPtzAdapter(transport=transport, clock=lambda: 10.0)
    binding = ViscaIpPtzBinding(host="camera.local")
    preset = CameraPreset(
        id="preset-1",
        camera_source_id="camera-1",
        name="Lectern",
        remote_token="7",
    )

    adapter.control(
        _control(
            binding,
            PtzControlKind.MOVE,
            pan=-0.5,
            tilt=0.25,
            zoom=0.75,
        ),
        PtzCancellation(),
    )
    adapter.control(_control(binding, PtzControlKind.STOP), PtzCancellation())
    adapter.control(
        _control(binding, PtzControlKind.STORE_PRESET, preset=preset),
        PtzCancellation(),
    )

    assert [call.command.hex(" ") for call in transport.calls] == [
        "81 01 06 01 0c 06 01 01 ff",
        "81 01 04 07 25 ff",
        "81 01 06 01 01 01 03 03 ff",
        "81 01 04 07 00 ff",
        "81 01 04 3f 01 07 ff",
    ]


def test_visca_rejects_normalized_absolute_position_without_device_ranges() -> None:
    adapter = ViscaIpPtzAdapter(transport=_IpTransport(), clock=lambda: 10.0)
    request = _request(
        ViscaIpPtzBinding(host="camera.local"),
        remote_token="",
        position=PtzPosition(pan=0.5, tilt=-0.5, zoom=0.25),
    )

    with pytest.raises(PtzExecutionError) as raised:
        adapter.recall(request, PtzCancellation())

    assert raised.value.error_code == "ptz_visca_absolute_position_unsupported"


def test_visca_ip_packet_and_replies_are_strictly_validated() -> None:
    command = bytes.fromhex("81 01 04 3f 02 07 ff")
    assert _ip_packet(0x0100, 9, command) == (
        struct.pack(">HHI", 0x0100, len(command), 9) + command
    )
    ack = _parse_ip_reply(
        struct.pack(">HHI", 0x0111, 3, 9) + bytes.fromhex("90 41 ff"),
        expected_sequence=9,
    )
    expected_socket, completed = _consume_reply(ack, None)
    assert expected_socket == 1
    assert not completed

    completion = _parse_visca_reply(
        bytes.fromhex("90 51 ff"),
        expected_address=0x90,
    )
    assert _consume_reply(completion, expected_socket) == (1, True)

    with pytest.raises(PtzExecutionError, match="ptz_visca_sequence_mismatch"):
        _parse_ip_reply(
            struct.pack(">HHI", 0x0111, 3, 10) + bytes.fromhex("90 41 ff"),
            expected_sequence=9,
        )


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ("90 60 02 ff", "ptz_visca_syntax_error"),
        ("90 61 03 ff", "ptz_visca_command_buffer_full"),
        ("90 61 41 ff", "ptz_visca_command_not_executable"),
    ],
)
def test_visca_camera_errors_are_typed(payload: str, error_code: str) -> None:
    reply = _parse_visca_reply(bytes.fromhex(payload), expected_address=0x90)
    with pytest.raises(PtzExecutionError) as raised:
        _consume_reply(reply, None)
    assert raised.value.error_code == error_code
