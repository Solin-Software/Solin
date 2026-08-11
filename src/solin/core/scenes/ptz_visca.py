"""VISCA-over-IP and VISCA-serial PTZ preset recall adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import socket
import struct
import threading
import time
from typing import Protocol

from solin.core.scenes.model import (
    PtzProtocol,
    ViscaIpPtzBinding,
    ViscaSerialPtzBinding,
    ViscaTransport,
)
from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzCancelledError,
    PtzControlKind,
    PtzControlRequest,
    PtzExecutionError,
    PtzRecallRequest,
)


_VISCA_IP_COMMAND = 0x0100
_VISCA_IP_RESPONSE = 0x0111
_VISCA_IP_CONTROL_RESPONSE = 0x0201
_VISCA_MAXIMUM_PAYLOAD_BYTES = 16
_VISCA_IP_HEADER_BYTES = 8
_UDP_RETRANSMIT_SECONDS = 0.25
_UDP_MAXIMUM_SENDS = 3
_CANCELLATION_POLL_SECONDS = 0.05


class ViscaIpTransport(Protocol):
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
    ) -> None: ...


class ViscaSerialTransport(Protocol):
    def transact(
        self,
        *,
        device_id: str,
        baud_rate: int,
        response_address: int,
        command: bytes,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> None: ...


class SocketViscaIpTransport:
    """Bounded VISCA-over-IP transport with UDP delivery confirmation."""

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
        if timeout_seconds <= 0:
            raise TimeoutError
        asyncio.run(
            self._transact(
                host=host,
                port=port,
                transport=transport,
                sequence=sequence,
                command=command,
                timeout_seconds=timeout_seconds,
                cancellation=cancellation,
            )
        )

    async def _transact(
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
        packet = _ip_packet(_VISCA_IP_COMMAND, sequence, command)
        try:
            async with asyncio.timeout(timeout_seconds):
                if transport is ViscaTransport.UDP:
                    await self._udp_transact(
                        host,
                        port,
                        sequence,
                        packet,
                        cancellation,
                    )
                else:
                    await self._tcp_transact(
                        host,
                        port,
                        sequence,
                        packet,
                        cancellation,
                    )
        except TimeoutError:
            raise
        except PtzCancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as error:
            raise PtzExecutionError("ptz_visca_network_unavailable") from error

    async def _udp_transact(
        self,
        host: str,
        port: int,
        sequence: int,
        packet: bytes,
        cancellation: PtzCancellation,
    ) -> None:
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_DGRAM,
        )
        if not addresses:
            raise PtzExecutionError("ptz_visca_host_not_found")
        family, socket_type, protocol, _canonical_name, address = addresses[0]
        udp_socket = socket.socket(family, socket_type, protocol)
        udp_socket.setblocking(False)
        try:
            await loop.sock_connect(udp_socket, address)
            sends = 1
            acknowledged = False
            expected_socket: int | None = None
            await loop.sock_sendall(udp_socket, packet)
            retransmit_at = loop.time() + _UDP_RETRANSMIT_SECONDS
            while True:
                _raise_if_cancelled(cancellation)
                timeout = _CANCELLATION_POLL_SECONDS
                if not acknowledged and sends < _UDP_MAXIMUM_SENDS:
                    timeout = min(timeout, max(0.0, retransmit_at - loop.time()))
                try:
                    response = await asyncio.wait_for(
                        loop.sock_recv(udp_socket, 64),
                        timeout=timeout,
                    )
                except TimeoutError:
                    if (
                        not acknowledged
                        and sends < _UDP_MAXIMUM_SENDS
                        and loop.time() >= retransmit_at
                    ):
                        await loop.sock_sendall(udp_socket, packet)
                        sends += 1
                        retransmit_at = loop.time() + _UDP_RETRANSMIT_SECONDS
                    continue
                reply = _parse_ip_reply(response, expected_sequence=sequence)
                expected_socket, completed = _consume_reply(reply, expected_socket)
                acknowledged = acknowledged or reply.kind == "ack"
                if completed:
                    return
        finally:
            udp_socket.close()

    async def _tcp_transact(
        self,
        host: str,
        port: int,
        sequence: int,
        packet: bytes,
        cancellation: PtzCancellation,
    ) -> None:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(packet)
            await writer.drain()
            expected_socket: int | None = None
            while True:
                header = await _read_exactly_with_cancellation(
                    reader,
                    _VISCA_IP_HEADER_BYTES,
                    cancellation,
                )
                payload_length = int.from_bytes(header[2:4], "big")
                if not 1 <= payload_length <= _VISCA_MAXIMUM_PAYLOAD_BYTES:
                    raise PtzExecutionError("ptz_visca_response_invalid")
                payload = await _read_exactly_with_cancellation(
                    reader,
                    payload_length,
                    cancellation,
                )
                reply = _parse_ip_reply(
                    header + payload,
                    expected_sequence=sequence,
                )
                expected_socket, completed = _consume_reply(reply, expected_socket)
                if completed:
                    return
        finally:
            writer.close()
            await writer.wait_closed()


class PyserialViscaSerialTransport:
    """Cross-platform serial transport; pyserial owns OS-specific port access."""

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
        if timeout_seconds <= 0:
            raise TimeoutError
        try:
            import serial
        except ImportError as error:
            raise PtzExecutionError("ptz_serial_runtime_unavailable") from error

        deadline = time.monotonic() + timeout_seconds
        try:
            with serial.Serial(
                port=device_id,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=_CANCELLATION_POLL_SECONDS,
                write_timeout=min(timeout_seconds, 1.0),
            ) as port:
                port.reset_input_buffer()
                port.write(command)
                port.flush()
                expected_socket: int | None = None
                buffer = bytearray()
                while True:
                    _raise_if_cancelled(cancellation)
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = port.read(1)
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    if len(buffer) > _VISCA_MAXIMUM_PAYLOAD_BYTES:
                        raise PtzExecutionError("ptz_visca_response_invalid")
                    if chunk != b"\xff":
                        continue
                    reply = _parse_visca_reply(
                        bytes(buffer),
                        expected_address=response_address,
                    )
                    buffer.clear()
                    expected_socket, completed = _consume_reply(reply, expected_socket)
                    if completed:
                        return
        except TimeoutError:
            raise
        except PtzCancelledError:
            raise
        except PtzExecutionError:
            raise
        except (OSError, ValueError) as error:
            raise PtzExecutionError("ptz_serial_unavailable") from error


class ViscaIpPtzAdapter:
    protocol = PtzProtocol.VISCA_IP

    def __init__(
        self,
        *,
        transport: ViscaIpTransport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, ViscaIpPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        preset_number = _preset_number(request)
        self._transport.transact(
            host=binding.host,
            port=binding.port,
            transport=binding.transport,
            sequence=self._next_sequence(),
            command=_preset_recall_command(1, preset_number),
            timeout_seconds=_remaining(request, self._clock),
            cancellation=cancellation,
        )

    def control(
        self,
        request: PtzControlRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, ViscaIpPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        for command in _control_commands(request, camera_address=1):
            self._transport.transact(
                host=binding.host,
                port=binding.port,
                transport=binding.transport,
                sequence=self._next_sequence(),
                command=command,
                timeout_seconds=_remaining(request, self._clock),
                cancellation=cancellation,
            )

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            return sequence


class ViscaSerialPtzAdapter:
    protocol = PtzProtocol.VISCA_SERIAL

    def __init__(
        self,
        *,
        transport: ViscaSerialTransport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._lock_guard = threading.Lock()
        self._device_locks: dict[str, threading.Lock] = {}

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, ViscaSerialPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        preset_number = _preset_number(request)
        lock = self._device_lock(binding.device_id)
        _acquire_before_deadline(lock, request, cancellation, self._clock)
        try:
            self._transport.transact(
                device_id=binding.device_id,
                baud_rate=binding.baud_rate,
                response_address=0x80 | (binding.camera_address + 8),
                command=_preset_recall_command(binding.camera_address, preset_number),
                timeout_seconds=_remaining(request, self._clock),
                cancellation=cancellation,
            )
        finally:
            lock.release()

    def control(
        self,
        request: PtzControlRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, ViscaSerialPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        lock = self._device_lock(binding.device_id)
        _acquire_before_deadline(lock, request, cancellation, self._clock)
        try:
            for command in _control_commands(
                request,
                camera_address=binding.camera_address,
            ):
                self._transport.transact(
                    device_id=binding.device_id,
                    baud_rate=binding.baud_rate,
                    response_address=0x80 | (binding.camera_address + 8),
                    command=command,
                    timeout_seconds=_remaining(request, self._clock),
                    cancellation=cancellation,
                )
        finally:
            lock.release()

    def _device_lock(self, device_id: str) -> threading.Lock:
        with self._lock_guard:
            return self._device_locks.setdefault(device_id, threading.Lock())


@dataclass(frozen=True, slots=True)
class _ViscaReply:
    kind: str
    socket_number: int
    error_code: int = 0


def _ip_packet(payload_type: int, sequence: int, payload: bytes) -> bytes:
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise ValueError("VISCA sequence is out of range")
    if not 1 <= len(payload) <= _VISCA_MAXIMUM_PAYLOAD_BYTES:
        raise ValueError("VISCA payload size is invalid")
    return struct.pack(">HHI", payload_type, len(payload), sequence) + payload


def _parse_ip_reply(packet: bytes, *, expected_sequence: int) -> _ViscaReply:
    if len(packet) < _VISCA_IP_HEADER_BYTES:
        raise PtzExecutionError("ptz_visca_response_invalid")
    payload_type, payload_length, sequence = struct.unpack(">HHI", packet[:8])
    if payload_length != len(packet) - _VISCA_IP_HEADER_BYTES:
        raise PtzExecutionError("ptz_visca_response_invalid")
    if sequence != expected_sequence:
        raise PtzExecutionError("ptz_visca_sequence_mismatch")
    if payload_type == _VISCA_IP_CONTROL_RESPONSE:
        raise PtzExecutionError("ptz_visca_control_error")
    if payload_type != _VISCA_IP_RESPONSE:
        raise PtzExecutionError("ptz_visca_response_invalid")
    return _parse_visca_reply(packet[8:], expected_address=0x90)


def _parse_visca_reply(payload: bytes, *, expected_address: int) -> _ViscaReply:
    if (
        not 3 <= len(payload) <= _VISCA_MAXIMUM_PAYLOAD_BYTES
        or payload[0] != expected_address
        or payload[-1] != 0xFF
    ):
        raise PtzExecutionError("ptz_visca_response_invalid")
    message_type = payload[1] & 0xF0
    socket_number = payload[1] & 0x0F
    if message_type == 0x40 and len(payload) == 3:
        return _ViscaReply("ack", socket_number)
    if message_type == 0x50 and len(payload) == 3:
        return _ViscaReply("completion", socket_number)
    if message_type == 0x60 and len(payload) == 4:
        return _ViscaReply("error", socket_number, payload[2])
    raise PtzExecutionError("ptz_visca_response_invalid")


def _consume_reply(
    reply: _ViscaReply,
    expected_socket: int | None,
) -> tuple[int | None, bool]:
    if reply.kind == "error":
        error_codes = {
            0x01: "ptz_visca_message_length_error",
            0x02: "ptz_visca_syntax_error",
            0x03: "ptz_visca_command_buffer_full",
            0x04: "ptz_visca_command_cancelled",
            0x05: "ptz_visca_socket_unavailable",
            0x41: "ptz_visca_command_not_executable",
        }
        raise PtzExecutionError(error_codes.get(reply.error_code, "ptz_visca_camera_error"))
    if reply.kind == "ack":
        if expected_socket is not None and expected_socket != reply.socket_number:
            raise PtzExecutionError("ptz_visca_socket_mismatch")
        return reply.socket_number, False
    if expected_socket is not None and expected_socket != reply.socket_number:
        raise PtzExecutionError("ptz_visca_socket_mismatch")
    return reply.socket_number, True


def _preset_number(request: PtzRecallRequest) -> int:
    if not request.preset.remote_token:
        if request.preset.position is not None:
            raise PtzExecutionError("ptz_visca_absolute_position_unsupported")
        raise PtzExecutionError("ptz_preset_target_required")
    try:
        preset_number = int(request.preset.remote_token, 0)
    except ValueError as error:
        raise PtzExecutionError("ptz_visca_preset_number_invalid") from error
    if not 0 <= preset_number <= 127:
        raise PtzExecutionError("ptz_visca_preset_number_invalid")
    return preset_number


def _preset_recall_command(camera_address: int, preset_number: int) -> bytes:
    if not 1 <= camera_address <= 7 or not 0 <= preset_number <= 127:
        raise ValueError("VISCA preset command arguments are invalid")
    return bytes((0x80 | camera_address, 0x01, 0x04, 0x3F, 0x02, preset_number, 0xFF))


def _preset_store_command(camera_address: int, preset_number: int) -> bytes:
    if not 1 <= camera_address <= 7 or not 0 <= preset_number <= 127:
        raise ValueError("VISCA preset command arguments are invalid")
    return bytes((0x80 | camera_address, 0x01, 0x04, 0x3F, 0x01, preset_number, 0xFF))


def _pan_tilt_command(
    camera_address: int,
    *,
    pan: float,
    tilt: float,
) -> bytes:
    pan_speed = max(1, min(24, round(abs(pan) * 24)))
    tilt_speed = max(1, min(23, round(abs(tilt) * 23)))
    pan_direction = 0x01 if pan < 0 else 0x02 if pan > 0 else 0x03
    tilt_direction = 0x01 if tilt > 0 else 0x02 if tilt < 0 else 0x03
    return bytes(
        (
            0x80 | camera_address,
            0x01,
            0x06,
            0x01,
            pan_speed,
            tilt_speed,
            pan_direction,
            tilt_direction,
            0xFF,
        )
    )


def _zoom_command(camera_address: int, zoom: float) -> bytes:
    if zoom == 0.0:
        direction_and_speed = 0x00
    else:
        speed = max(0, min(7, round(abs(zoom) * 7)))
        direction_and_speed = (0x20 if zoom > 0 else 0x30) | speed
    return bytes(
        (0x80 | camera_address, 0x01, 0x04, 0x07, direction_and_speed, 0xFF)
    )


def _control_commands(
    request: PtzControlRequest,
    *,
    camera_address: int,
) -> tuple[bytes, ...]:
    if request.kind is PtzControlKind.STORE_PRESET:
        if request.preset is None:
            raise PtzExecutionError("ptz_preset_target_required")
        return (
            _preset_store_command(
                camera_address,
                _preset_number_from_token(request.preset.remote_token),
            ),
        )
    if request.kind is PtzControlKind.STOP:
        return (
            _pan_tilt_command(camera_address, pan=0.0, tilt=0.0),
            _zoom_command(camera_address, 0.0),
        )
    commands = []
    if request.pan or request.tilt:
        commands.append(
            _pan_tilt_command(
                camera_address,
                pan=request.pan,
                tilt=request.tilt,
            )
        )
    if request.zoom:
        commands.append(_zoom_command(camera_address, request.zoom))
    return tuple(commands)


def _preset_number_from_token(remote_token: str) -> int:
    if not remote_token:
        raise PtzExecutionError("ptz_preset_target_required")
    try:
        preset_number = int(remote_token, 0)
    except ValueError as error:
        raise PtzExecutionError("ptz_visca_preset_number_invalid") from error
    if not 0 <= preset_number <= 127:
        raise PtzExecutionError("ptz_visca_preset_number_invalid")
    return preset_number


def _remaining(
    request: PtzRecallRequest | PtzControlRequest,
    clock: Callable[[], float],
) -> float:
    remaining = request.remaining_seconds(clock)
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _raise_if_cancelled(cancellation: PtzCancellation) -> None:
    if cancellation.cancelled:
        raise PtzCancelledError


async def _read_exactly_with_cancellation(
    reader: asyncio.StreamReader,
    size: int,
    cancellation: PtzCancellation,
) -> bytes:
    task = asyncio.create_task(reader.readexactly(size))
    try:
        while not task.done():
            _raise_if_cancelled(cancellation)
            await asyncio.wait({task}, timeout=_CANCELLATION_POLL_SECONDS)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _acquire_before_deadline(
    lock: threading.Lock,
    request: PtzRecallRequest | PtzControlRequest,
    cancellation: PtzCancellation,
    clock: Callable[[], float],
) -> None:
    while True:
        _raise_if_cancelled(cancellation)
        remaining = request.remaining_seconds(clock)
        if remaining <= 0:
            raise TimeoutError
        if lock.acquire(timeout=min(_CANCELLATION_POLL_SECONDS, remaining)):
            return
