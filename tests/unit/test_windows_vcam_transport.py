"""Byte-exactness tests for the Windows vcam wire + ring formats.

The reader helpers here mirror the C++ ``decode_virtual_camera_broker_response``,
``parse_header`` and ``read_current_slot`` in native/media_engine, so a passing
round-trip proves the Python producer emits bytes the shipped DirectShow filter
accepts.
"""
from __future__ import annotations

import struct

import pytest

from solin.core.scenes.windows_vcam_transport import (
    RESPONSE_SIZE,
    BrokerProtocolError,
    BrokerStatus,
    PixelFormat,
    SharedFrameRingWriter,
    aligned_slot_size,
    decode_request,
    encode_request,
    encode_response,
    mapping_size_for,
    nv12_layout,
    pack_nv12,
)


# ── broker protocol ──────────────────────────────────────────────────────────


def test_request_roundtrip_and_magic():
    nonce = bytes(range(16))
    frame = encode_request(nonce)
    assert len(frame) == 32 and frame[0:8] == b"SLNBRK03"
    assert struct.unpack_from("<HHI", frame, 8) == (3, 32, 0)
    assert decode_request(frame) == nonce


def test_request_rejects_tampered_frame():
    frame = bytearray(encode_request(bytes(16)))
    frame[8] = 4  # wrong version
    with pytest.raises(BrokerProtocolError):
        decode_request(bytes(frame))


def _decode_response(frame: bytes) -> dict:
    """Mirror of the C++ decode_virtual_camera_broker_response cross-checks."""
    assert len(frame) == RESPONSE_SIZE and frame[0:8] == b"SLNBRP03"
    version, size, status = struct.unpack_from("<HHI", frame, 8)
    assert version == 3 and size == RESPONSE_SIZE
    nonce = bytes(frame[16:32])
    if status != 0:
        assert all(b == 0 for b in frame[32:])  # error frames are zero past the nonce
        return {"status": status, "nonce": nonce}
    mapping_size, generation, payload_size = struct.unpack_from("<QQQ", frame, 32)
    width, height, pixel_format, plane_count = struct.unpack_from("<IIII", frame, 56)
    stride0, stride1 = struct.unpack_from("<II", frame, 72)
    (offset1,) = struct.unpack_from("<Q", frame, 80)
    fps_num, fps_den = struct.unpack_from("<II", frame, 88)
    (path_len,) = struct.unpack_from("<H", frame, 96)
    assert all(b == 0 for b in frame[98:128])  # padding
    path = frame[128:128 + path_len].decode("utf-8")
    assert all(b == 0 for b in frame[128 + path_len:])
    layout = nv12_layout(width, height)
    assert pixel_format == int(PixelFormat.NV12)
    assert payload_size == layout.payload_size
    assert plane_count == layout.plane_count
    assert (stride0, stride1) == layout.plane_strides
    assert offset1 == layout.plane_offsets[1]
    return {
        "status": status, "nonce": nonce, "mapping_size": mapping_size,
        "generation": generation, "path": path, "fps": (fps_num, fps_den),
        "width": width, "height": height,
    }


def test_ok_response_roundtrips_through_a_faithful_reader():
    layout = nv12_layout(1280, 720)
    frame = encode_response(
        status=BrokerStatus.OK, nonce=bytes(range(16)),
        mapping_file_path_utf8=r"C:\Users\x\AppData\Local\Temp\Solin.VirtualCamera.{GUID}.frames",
        mapping_size=mapping_size_for(layout), generation=7, layout=layout,
        fps_numerator=30, fps_denominator=1,
    )
    decoded = _decode_response(frame)
    assert decoded["status"] == 0 and decoded["generation"] == 7
    assert decoded["fps"] == (30, 1) and decoded["width"] == 1280
    assert decoded["path"].endswith(".frames")
    assert decoded["mapping_size"] == mapping_size_for(layout)


def test_error_response_is_zero_past_the_nonce():
    frame = encode_response(status=BrokerStatus.UNAUTHORIZED, nonce=bytes(16))
    decoded = _decode_response(frame)
    assert decoded["status"] == int(BrokerStatus.UNAUTHORIZED)


# ── NV12 layout + mapping size (matches the C++ example) ──────────────────────


def test_nv12_layout_and_mapping_size():
    layout = nv12_layout(1280, 720)
    assert layout.plane_strides == (1280, 1280)
    assert layout.plane_offsets == (0, 1280 * 720)
    assert layout.payload_size == 1280 * 720 * 3 // 2 == 1382400
    assert aligned_slot_size(layout.payload_size) == 1382464
    assert mapping_size_for(layout) == 128 + 1382464 * 3 == 4147520


def test_nv12_layout_rejects_odd_dimensions():
    with pytest.raises(ValueError):
        nv12_layout(1281, 720)


# ── shared-memory frame ring (SLNVDO01) ──────────────────────────────────────


def _read_ring_header(buf: bytes) -> dict:
    """Mirror of the C++ parse_header validation."""
    assert buf[0:8] == b"SLNVDO01"
    version, header_size = struct.unpack_from("<HH", buf, 8)
    (slot_count,) = struct.unpack_from("<I", buf, 12)
    assert version == 2 and header_size == 128 and slot_count == 3
    width, height, pixel_format, plane_count = struct.unpack_from("<IIII", buf, 16)
    stride0, stride1 = struct.unpack_from("<II", buf, 32)
    (offset1,) = struct.unpack_from("<Q", buf, 40)
    generation, payload_size, slot_size, mapping_size = struct.unpack_from("<QQQQ", buf, 48)
    assert generation != 0
    layout = nv12_layout(width, height)
    assert pixel_format == int(PixelFormat.NV12)
    assert (stride0, stride1) == layout.plane_strides
    assert offset1 == layout.plane_offsets[1]
    assert payload_size == layout.payload_size
    assert slot_size == aligned_slot_size(payload_size)
    assert mapping_size == mapping_size_for(layout)
    return {"payload_size": payload_size, "slot_size": slot_size,
            "generation": generation, "width": width, "height": height}


def _read_current_slot(buf: bytes, slot_size: int, payload_size: int) -> bytes | None:
    (published,) = struct.unpack_from("<Q", buf, 80)
    if published == 0:
        return None
    slot = 128 + ((published - 1) % 3) * slot_size
    (marker,) = struct.unpack_from("<Q", buf, slot)
    if marker != published * 2:  # not a complete slot for this sequence
        return None
    (seq,) = struct.unpack_from("<Q", buf, slot + 8)
    if seq != published:
        return None
    return bytes(buf[slot + 64:slot + 64 + payload_size])


def test_ring_writer_publishes_a_frame_a_faithful_reader_can_read():
    width, height = 16, 8
    layout = nv12_layout(width, height)
    buf = bytearray(mapping_size_for(layout))
    writer = SharedFrameRingWriter(buf, layout, generation=42)

    header = _read_ring_header(bytes(buf))
    assert header["generation"] == 42 and header["width"] == 16

    payload = bytes((i % 251) for i in range(layout.payload_size))
    seq = writer.write(payload)
    assert seq == 1
    frame = _read_current_slot(bytes(buf), header["slot_size"], header["payload_size"])
    assert frame == payload

    # a second frame lands in the next slot and becomes the current one
    payload2 = bytes(((i + 7) % 251) for i in range(layout.payload_size))
    writer.write(payload2)
    assert _read_current_slot(bytes(buf), header["slot_size"], header["payload_size"]) == payload2


def test_ring_heartbeat_advances_without_a_frame():
    layout = nv12_layout(16, 8)
    buf = bytearray(mapping_size_for(layout))
    SharedFrameRingWriter(buf, layout, generation=1)
    (hb0,) = struct.unpack_from("<Q", buf, 88)
    writer = SharedFrameRingWriter(buf, layout, generation=1)
    writer.heartbeat()
    (hb1,) = struct.unpack_from("<Q", buf, 88)
    assert hb1 == hb0 + 1


def test_pack_nv12_tightens_padded_strides():
    width, height = 4, 2
    y = bytes([1, 2, 3, 4, 0, 0] * height)   # y_stride 6 (padded)
    uv = bytes([9, 8, 7, 6, 0, 0])           # uv_stride 6, height//2 = 1 row
    packed = pack_nv12(y, 6, uv, 6, width, height)
    assert packed == bytes([1, 2, 3, 4, 1, 2, 3, 4, 9, 8, 7, 6])
    assert len(packed) == width * height + (width * height) // 2


def test_pack_nv12_tight_fast_path_matches_padded_path():
    width, height = 4, 2
    y = bytes([1, 2, 3, 4, 5, 6, 7, 8])      # y_stride == width (tight)
    uv = bytes([9, 8, 7, 6])                  # uv_stride == width, 1 chroma row
    packed = pack_nv12(y, width, uv, width, width, height)
    assert packed == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 8, 7, 6])
    # over-long plane buffers (as pylibobs may hand over) are trimmed to size
    padded_tail = pack_nv12(y + b"\x00\x00", width, uv + b"\x00\x00", width, width, height)
    assert padded_tail == packed
