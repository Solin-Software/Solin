from __future__ import annotations

import io
import json
import struct

import pytest

from solin.core.scenes.ipc_protocol import (
    MAXIMUM_CONTROL_FRAME_BYTES,
    PROTOCOL_VERSION,
    SceneIpcEnvelope,
    SceneIpcFrameError,
    SceneIpcMessageError,
    decode_envelope,
    encode_envelope,
    read_envelope,
)


def _envelope(**overrides: object) -> SceneIpcEnvelope:
    values: dict[str, object] = {
        "message_type": "hello",
        "request_id": "request-1",
        "session_id": "session-1",
        "process_generation": "generation-1",
        "sequence": 1,
        "document_revision": 2,
        "deadline_monotonic_ms": 3000,
        "payload": {"probe": True},
    }
    values.update(overrides)
    return SceneIpcEnvelope(**values)


def test_envelope_round_trips_through_a_fragmented_stream() -> None:
    expected = _envelope(payload={"name": "Câmera", "enabled": True})
    stream = _FragmentedStream(encode_envelope(expected), fragment_size=3)

    assert read_envelope(stream) == expected
    assert read_envelope(stream) is None


def test_decoder_rejects_duplicate_and_unknown_envelope_fields() -> None:
    record = _envelope().to_record()
    encoded = json.dumps(record, separators=(",", ":"))
    duplicate = encoded.replace('"payload":', '"sequence":9,"payload":')
    with pytest.raises(SceneIpcMessageError, match="duplicate"):
        decode_envelope(duplicate.encode())

    record["unexpected"] = True
    with pytest.raises(SceneIpcMessageError, match="fields"):
        decode_envelope(json.dumps(record).encode())


def test_decoder_rejects_a_sidecar_from_the_previous_protocol_generation() -> None:
    record = _envelope().to_record()
    record["protocol_version"] = PROTOCOL_VERSION - 1

    with pytest.raises(SceneIpcMessageError, match="protocol version"):
        decode_envelope(json.dumps(record).encode())


def test_decoder_rejects_non_finite_json_and_invalid_utf8() -> None:
    encoded = json.dumps(_envelope().to_record()).replace("true", "NaN", 1)
    with pytest.raises(SceneIpcMessageError, match="constant"):
        decode_envelope(encoded.encode())
    with pytest.raises(SceneIpcMessageError, match="UTF-8"):
        decode_envelope(b"\xff")


def test_reader_rejects_oversized_and_truncated_frames_before_decoding() -> None:
    oversized = struct.pack(">I", MAXIMUM_CONTROL_FRAME_BYTES + 1)
    with pytest.raises(SceneIpcFrameError, match="exceeds"):
        read_envelope(io.BytesIO(oversized))

    truncated = struct.pack(">I", 5) + b"abc"
    with pytest.raises(SceneIpcFrameError, match="truncated"):
        read_envelope(io.BytesIO(truncated))


def test_encoder_rejects_non_json_payload_values() -> None:
    with pytest.raises(SceneIpcMessageError, match="strict JSON"):
        encode_envelope(_envelope(payload={"invalid": object()}))


class _FragmentedStream:
    def __init__(self, value: bytes, *, fragment_size: int) -> None:
        self._stream = io.BytesIO(value)
        self._fragment_size = fragment_size

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(min(size, self._fragment_size))
