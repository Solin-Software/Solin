from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import BinaryIO, Final, cast


PROTOCOL_VERSION: Final = 4
MAXIMUM_CONTROL_FRAME_BYTES: Final = 8 * 1024 * 1024
_FRAME_HEADER = struct.Struct(">I")
_MESSAGE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENVELOPE_FIELDS = frozenset(
    {
        "protocol_version",
        "message_type",
        "request_id",
        "session_id",
        "process_generation",
        "sequence",
        "document_revision",
        "deadline_monotonic_ms",
        "payload",
    }
)


class SceneIpcError(ValueError):
    """Base class for invalid or unsupported control-plane data."""


class SceneIpcFrameError(SceneIpcError):
    """Raised when a length-prefixed frame is malformed."""


class SceneIpcMessageError(SceneIpcError):
    """Raised when a control message violates the protocol contract."""


@dataclass(frozen=True, slots=True)
class SceneIpcEnvelope:
    message_type: str
    request_id: str
    session_id: str
    process_generation: str
    sequence: int
    document_revision: int
    deadline_monotonic_ms: int
    payload: dict[str, object]
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise SceneIpcMessageError("Unsupported scene IPC protocol version")
        if not isinstance(self.message_type, str) or not _MESSAGE_TYPE.fullmatch(self.message_type):
            raise SceneIpcMessageError("Invalid scene IPC message type")
        _bounded_identity(self.request_id, "request id")
        _bounded_identity(self.session_id, "session id")
        _bounded_identity(self.process_generation, "process generation")
        _non_negative_int(self.sequence, "sequence")
        _non_negative_int(self.document_revision, "document revision")
        _non_negative_int(self.deadline_monotonic_ms, "deadline")
        if not isinstance(self.payload, dict) or not all(
            isinstance(key, str) for key in self.payload
        ):
            raise SceneIpcMessageError("Scene IPC payload must be an object")

    def to_record(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "process_generation": self.process_generation,
            "sequence": self.sequence,
            "document_revision": self.document_revision,
            "deadline_monotonic_ms": self.deadline_monotonic_ms,
            "payload": self.payload,
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneIpcEnvelope:
        if not isinstance(raw, dict) or set(raw) != _ENVELOPE_FIELDS:
            raise SceneIpcMessageError("Invalid scene IPC envelope fields")
        return cls(
            protocol_version=raw["protocol_version"],
            message_type=raw["message_type"],
            request_id=raw["request_id"],
            session_id=raw["session_id"],
            process_generation=raw["process_generation"],
            sequence=raw["sequence"],
            document_revision=raw["document_revision"],
            deadline_monotonic_ms=raw["deadline_monotonic_ms"],
            payload=raw["payload"],
        )


def encode_envelope(envelope: SceneIpcEnvelope) -> bytes:
    try:
        payload = json.dumps(
            envelope.to_record(),
            ensure_ascii=False,
            allow_nan=False,
            check_circular=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SceneIpcMessageError("Scene IPC message is not strict JSON") from exc
    if len(payload) > MAXIMUM_CONTROL_FRAME_BYTES:
        raise SceneIpcFrameError("Scene IPC frame exceeds the protocol limit")
    return _FRAME_HEADER.pack(len(payload)) + payload


def decode_envelope(payload: bytes) -> SceneIpcEnvelope:
    if len(payload) > MAXIMUM_CONTROL_FRAME_BYTES:
        raise SceneIpcFrameError("Scene IPC frame exceeds the protocol limit")
    try:
        text = payload.decode("utf-8", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SceneIpcMessageError("Scene IPC payload is not strict UTF-8 JSON") from exc
    return SceneIpcEnvelope.from_record(raw)


def read_envelope(stream: BinaryIO) -> SceneIpcEnvelope | None:
    header = _read_exact(stream, _FRAME_HEADER.size, clean_eof=True)
    if header is None:
        return None
    (payload_size,) = _FRAME_HEADER.unpack(header)
    if payload_size > MAXIMUM_CONTROL_FRAME_BYTES:
        raise SceneIpcFrameError("Scene IPC frame exceeds the protocol limit")
    payload = _read_exact(stream, payload_size, clean_eof=False)
    if payload is None:
        raise SceneIpcFrameError("Scene IPC frame payload is truncated")
    return decode_envelope(payload)


def write_envelope(stream: BinaryIO, envelope: SceneIpcEnvelope) -> None:
    frame = encode_envelope(envelope)
    written = stream.write(frame)
    if written is not None and written != len(frame):
        raise SceneIpcFrameError("Scene IPC frame write is incomplete")
    stream.flush()


def require_payload_fields(
    payload: object,
    required_fields: frozenset[str],
    *,
    message_type: str,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != required_fields:
        raise SceneIpcMessageError(f"Invalid {message_type} payload fields")
    return payload


def require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise SceneIpcMessageError(f"Invalid {field_name}")
    return value


def require_non_negative_int(value: object, field_name: str) -> int:
    _non_negative_int(value, field_name)
    return cast(int, value)


def require_text(value: object, field_name: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise SceneIpcMessageError(f"Invalid {field_name}")
    return value


def _read_exact(stream: BinaryIO, size: int, *, clean_eof: bool) -> bytes | None:
    remaining = size
    chunks: list[bytes] = []
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if clean_eof and remaining == size:
                return None
            raise SceneIpcFrameError("Scene IPC frame is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SceneIpcMessageError("Scene IPC JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SceneIpcMessageError(f"Unsupported JSON constant: {value}")


def _bounded_identity(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise SceneIpcMessageError(f"Invalid {field_name}")


def _non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise SceneIpcMessageError(f"Invalid {field_name}")
