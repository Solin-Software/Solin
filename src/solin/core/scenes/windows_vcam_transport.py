"""Wire + shared-memory formats for the Windows virtual-camera feed.

Pure-Python, byte-exact reimplementations of the formats the shipped Solin
DirectShow filter DLL already speaks, so the libobs sidecar can act as the
broker + frame producer on Windows (the native GStreamer engine normally plays
that role, but it does not run under the libobs engine). Mirrors:

* ``native/media_engine/src/virtual_camera_broker_protocol.cpp`` — the v3
  broker request/response frames exchanged over the per-user named pipe.
* ``native/media_engine/src/shared_video_frame_channel.cpp`` +
  ``video_frame.cpp`` — the ``SLNVDO01`` shared-memory frame ring (128-byte
  header + 3 seqlock slots) the filter reads.

These are platform-neutral (they operate on ``bytes`` / a writable buffer), so
they are unit-tested on any OS; the Windows-only glue (named pipe, SID, file
mapping) lives in the sibling modules.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# ── broker pipe protocol (v3) ────────────────────────────────────────────────

BROKER_PROTOCOL_VERSION = 3
REQUEST_MAGIC = b"SLNBRK03"
RESPONSE_MAGIC = b"SLNBRP03"
REQUEST_SIZE = 32
RESPONSE_SIZE = 640
NONCE_SIZE = 16
MAX_PATH_BYTES = 512
PRESENCE_MARKER = 0xA5  # byte a persistent filter streams to keep the pipe open


class BrokerStatus(IntEnum):
    OK = 0
    INVALID_REQUEST = 1
    PROTOCOL_MISMATCH = 2
    UNAUTHORIZED = 3
    TRANSPORT_UNAVAILABLE = 4
    SHUTTING_DOWN = 5


# ── pixel format + packed layout (mirrors video_frame.cpp) ───────────────────

class PixelFormat(IntEnum):
    BGRA = 1
    NV12 = 2
    YUY2 = 3


@dataclass(frozen=True)
class PackedVideoFrameLayout:
    width: int
    height: int
    pixel_format: PixelFormat
    plane_strides: tuple[int, int]
    plane_offsets: tuple[int, int]
    plane_count: int
    payload_size: int


def nv12_layout(width: int, height: int) -> PackedVideoFrameLayout:
    """NV12 layout: Y then interleaved UV, tight (stride == width, even dims)."""
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("NV12 requires positive even width and height")
    pixels = width * height
    return PackedVideoFrameLayout(
        width=width,
        height=height,
        pixel_format=PixelFormat.NV12,
        plane_strides=(width, width),
        plane_offsets=(0, pixels),
        plane_count=2,
        payload_size=pixels + pixels // 2,
    )


class BrokerProtocolError(ValueError):
    """A broker frame violated the v3 contract."""


def encode_request(nonce: bytes) -> bytes:
    if len(nonce) != NONCE_SIZE:
        raise BrokerProtocolError("nonce must be 16 bytes")
    frame = bytearray(REQUEST_SIZE)
    frame[0:8] = REQUEST_MAGIC
    struct.pack_into("<HH", frame, 8, BROKER_PROTOCOL_VERSION, REQUEST_SIZE)
    # bytes 12-15 stay zero (reserved); 16..31 carry the nonce.
    frame[16:32] = nonce
    return bytes(frame)


def decode_request(frame: bytes) -> bytes:
    """Validate a request frame; return its 16-byte nonce."""
    if len(frame) != REQUEST_SIZE or frame[0:8] != REQUEST_MAGIC:
        raise BrokerProtocolError("invalid broker request frame")
    version, size, reserved = struct.unpack_from("<HHI", frame, 8)
    if version != BROKER_PROTOCOL_VERSION or size != REQUEST_SIZE or reserved != 0:
        raise BrokerProtocolError("invalid broker request frame")
    return bytes(frame[16:32])


def encode_response(
    *,
    status: BrokerStatus,
    nonce: bytes,
    mapping_file_path_utf8: str = "",
    mapping_size: int = 0,
    generation: int = 0,
    layout: PackedVideoFrameLayout | None = None,
    fps_numerator: int = 0,
    fps_denominator: int = 0,
) -> bytes:
    if len(nonce) != NONCE_SIZE:
        raise BrokerProtocolError("nonce must be 16 bytes")
    frame = bytearray(RESPONSE_SIZE)
    frame[0:8] = RESPONSE_MAGIC
    struct.pack_into("<HHI", frame, 8, BROKER_PROTOCOL_VERSION, RESPONSE_SIZE, int(status))
    frame[16:32] = nonce
    if status != BrokerStatus.OK:
        return bytes(frame)  # error responses are zero past the nonce
    if layout is None:
        raise BrokerProtocolError("an ok response requires a layout")
    path = mapping_file_path_utf8.encode("utf-8")
    if not path or len(path) > MAX_PATH_BYTES or b"\0" in path:
        raise BrokerProtocolError("invalid mapping path")
    struct.pack_into("<Q", frame, 32, mapping_size)
    struct.pack_into("<Q", frame, 40, generation)
    struct.pack_into("<Q", frame, 48, layout.payload_size)
    struct.pack_into("<II", frame, 56, layout.width, layout.height)
    struct.pack_into("<II", frame, 64, int(layout.pixel_format), layout.plane_count)
    struct.pack_into("<II", frame, 72, layout.plane_strides[0], layout.plane_strides[1])
    struct.pack_into("<Q", frame, 80, layout.plane_offsets[1])
    struct.pack_into("<II", frame, 88, fps_numerator, fps_denominator)
    struct.pack_into("<H", frame, 96, len(path))
    frame[128:128 + len(path)] = path
    return bytes(frame)


# ── shared-memory frame ring (SLNVDO01) ──────────────────────────────────────

RING_MAGIC = b"SLNVDO01"
RING_VERSION = 2
RING_HEADER_SIZE = 128
RING_SLOT_HEADER_SIZE = 64
RING_SLOT_COUNT = 3
RING_MAX_SEQUENCE = (1 << 63) // 1 - 1  # INT64_MAX/2
_PUBLISHED_SEQUENCE_OFFSET = 80
_HEARTBEAT_OFFSET = 88
_MAX_SEQUENCE = 4611686018427387903  # INT64_MAX // 2


def aligned_slot_size(payload_size: int) -> int:
    return (RING_SLOT_HEADER_SIZE + payload_size + 63) & ~63


def mapping_size_for(layout: PackedVideoFrameLayout) -> int:
    return RING_HEADER_SIZE + aligned_slot_size(layout.payload_size) * RING_SLOT_COUNT


class SharedFrameRingWriter:
    """Writes NV12 frames into an ``SLNVDO01`` ring in a writable buffer.

    ``buffer`` is any writable bytes-like of at least :func:`mapping_size_for`
    bytes (an ``mmap`` of the shared file on Windows, or a ``bytearray`` in
    tests). The header is written on construction; :meth:`write` publishes one
    frame using the seqlock the DirectShow filter expects.
    """

    def __init__(self, buffer, layout: PackedVideoFrameLayout, *, generation: int) -> None:
        if generation <= 0:
            raise ValueError("generation must be non-zero")
        needed = mapping_size_for(layout)
        if len(buffer) < needed:
            raise ValueError("buffer smaller than the mapping size")
        self._buf = buffer
        self._layout = layout
        self._slot_size = aligned_slot_size(layout.payload_size)
        self._mapping_size = needed
        self._sequence = 0
        self._heartbeat = 1
        self._write_header(generation)

    @property
    def mapping_size(self) -> int:
        return self._mapping_size

    def _write_header(self, generation: int) -> None:
        buf = self._buf
        buf[0:RING_HEADER_SIZE] = bytes(RING_HEADER_SIZE)
        buf[0:8] = RING_MAGIC
        struct.pack_into("<HH", buf, 8, RING_VERSION, RING_HEADER_SIZE)
        struct.pack_into("<I", buf, 12, RING_SLOT_COUNT)
        struct.pack_into("<II", buf, 16, self._layout.width, self._layout.height)
        struct.pack_into("<II", buf, 24, int(self._layout.pixel_format), self._layout.plane_count)
        struct.pack_into("<II", buf, 32, self._layout.plane_strides[0], self._layout.plane_strides[1])
        struct.pack_into("<Q", buf, 40, self._layout.plane_offsets[1])
        struct.pack_into("<Q", buf, 48, generation)
        struct.pack_into("<Q", buf, 56, self._layout.payload_size)
        struct.pack_into("<Q", buf, 64, self._slot_size)
        struct.pack_into("<Q", buf, 72, self._mapping_size)
        struct.pack_into("<Q", buf, _PUBLISHED_SEQUENCE_OFFSET, 0)
        struct.pack_into("<Q", buf, _HEARTBEAT_OFFSET, self._heartbeat)

    def write(
        self,
        payload: bytes,
        *,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
        produced_monotonic_ns: int = 0,
        discontinuity: bool = False,
    ) -> int:
        """Publish one packed NV12 frame (``payload_size`` bytes); return its sequence."""
        if len(payload) != self._layout.payload_size:
            raise ValueError("payload size mismatch")
        sequence = self._sequence + 1
        if sequence > _MAX_SEQUENCE:
            sequence = 1
        self._sequence = sequence
        slot_index = (sequence - 1) % RING_SLOT_COUNT
        slot = RING_HEADER_SIZE + slot_index * self._slot_size
        buf = self._buf
        # seqlock: mark the slot as being written (odd), fill it, mark complete
        # (even), then advance the global published sequence — ordering the reader
        # relies on (x86 preserves store order).
        struct.pack_into("<Q", buf, slot, sequence * 2 - 1)
        struct.pack_into("<Q", buf, slot + 8, sequence)
        struct.pack_into("<Q", buf, slot + 16, presentation_timestamp_ns)
        struct.pack_into("<Q", buf, slot + 24, duration_ns)
        struct.pack_into("<Q", buf, slot + 32, produced_monotonic_ns)
        struct.pack_into("<Q", buf, slot + 40, self._layout.payload_size)
        struct.pack_into("<I", buf, slot + 48, 1 if discontinuity else 0)
        buf[slot + RING_SLOT_HEADER_SIZE:slot + RING_SLOT_HEADER_SIZE + len(payload)] = payload
        struct.pack_into("<Q", buf, slot, sequence * 2)
        struct.pack_into("<Q", buf, _PUBLISHED_SEQUENCE_OFFSET, sequence)
        self._beat()
        return sequence

    def heartbeat(self) -> None:
        """Advance the liveness counter without publishing a frame."""
        self._beat()

    def _beat(self) -> None:
        self._heartbeat = 1 if self._heartbeat >= _MAX_SEQUENCE else self._heartbeat + 1
        struct.pack_into("<Q", self._buf, _HEARTBEAT_OFFSET, self._heartbeat)


def pack_nv12(y_plane: bytes, y_stride: int, uv_plane: bytes, uv_stride: int,
              width: int, height: int) -> bytes:
    """Tightly pack (stride==width) Y + interleaved UV planes for the ring."""
    y_bytes = width * height
    uv_bytes = y_bytes // 2
    if y_stride == width and uv_stride == width:
        # Already tight: two contiguous memcpys, no per-row Python loop.
        return bytes(y_plane[:y_bytes]) + bytes(uv_plane[:uv_bytes])
    out = bytearray(y_bytes + uv_bytes)
    pos = 0
    for row in range(height):
        start = row * y_stride
        out[pos:pos + width] = y_plane[start:start + width]
        pos += width
    for row in range(height // 2):
        start = row * uv_stride
        out[pos:pos + width] = uv_plane[start:start + width]
        pos += width
    return bytes(out)
