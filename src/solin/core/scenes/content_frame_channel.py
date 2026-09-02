"""Cross-platform shared-memory frame channel for content ingress.

The app renders content that cannot be an ``ffmpeg_source`` (yeartext, timers,
the live browser, framed images) to BGRA and hands it to the libobs sidecar so it
composites like any other source. main's transport is Windows-only shared memory
+ D3D11; this is the cross-platform equivalent built on
:class:`multiprocessing.shared_memory.SharedMemory` (POSIX shm on Linux/macOS, a
named file mapping on Windows).

One writer (the app) and one reader (the sidecar) share a fixed-size block: a
small header + a BGRA payload. A seqlock (an even/odd sequence counter) lets the
reader take a tear-free snapshot without locking the writer:

    writer: seq→odd (writing) → write dims+payload → seq→even (done)
    reader: read seq → copy payload → read seq again; keep it only if both reads
            are equal and even (otherwise the writer was mid-frame; skip).

The reader also skips frames it has already delivered, so the consumer only
pushes genuinely new frames into libobs.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from multiprocessing import shared_memory

_MAGIC = b"SFC1"
_HEADER_SIZE = 64
# Header layout (little-endian): magic@0 (4s), seq@8 (uint64), width@16 (uint32),
# height@20 (uint32), stride@24 (uint32), format@28 (uint32; 0 = BGRA).
_SEQ_OFFSET = 8
_DIMS_OFFSET = 16
_FORMAT_OFFSET = 28
_FORMAT_BGRA = 0


@dataclass(frozen=True, slots=True)
class ContentFrame:
    """One BGRA snapshot read from the channel."""

    data: bytes
    width: int
    height: int
    stride: int


def _payload_bytes(width: int, height: int) -> int:
    return width * height * 4


class SharedFrameChannelWriter:
    """Producer side (the app). Creates the shared block and writes frames."""

    def __init__(self, width: int, height: int, *, name: str | None = None) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        self._width = width
        self._height = height
        self._payload = _payload_bytes(width, height)
        self._shm = shared_memory.SharedMemory(
            create=True, size=_HEADER_SIZE + self._payload, name=name
        )
        self._buf = self._shm.buf
        self._version = 0
        struct.pack_into("<4s", self._buf, 0, _MAGIC)
        struct.pack_into("<Q", self._buf, _SEQ_OFFSET, 0)
        struct.pack_into("<III", self._buf, _DIMS_OFFSET, width, height, width * 4)
        struct.pack_into("<I", self._buf, _FORMAT_OFFSET, _FORMAT_BGRA)

    @property
    def name(self) -> str:
        return self._shm.name

    def write(self, data: bytes, *, stride: int | None = None) -> None:
        """Publish one BGRA frame (tear-free via the seqlock)."""
        stride = self._width * 4 if stride is None else int(stride)
        count = min(len(data), self._payload)
        writing = self._version + 1  # odd → a write is in progress
        struct.pack_into("<Q", self._buf, _SEQ_OFFSET, writing)
        struct.pack_into("<III", self._buf, _DIMS_OFFSET, self._width, self._height, stride)
        self._buf[_HEADER_SIZE:_HEADER_SIZE + count] = data[:count]
        done = writing + 1  # even → complete
        struct.pack_into("<Q", self._buf, _SEQ_OFFSET, done)
        self._version = done

    def close(self) -> None:
        buf, self._buf = self._buf, None
        if buf is not None:
            buf.release()
        self._shm.close()

    def unlink(self) -> None:
        self._shm.unlink()


class SharedFrameChannelReader:
    """Consumer side (the sidecar). Attaches to an existing block by name."""

    def __init__(self, name: str, width: int, height: int) -> None:
        self._shm = shared_memory.SharedMemory(name=name)
        self._buf = self._shm.buf
        self._width = width
        self._height = height
        self._payload = _payload_bytes(width, height)
        self._last_seq = 0
        if bytes(self._buf[0:4]) != _MAGIC:
            self.close()
            raise ValueError("not a Solin content frame channel")

    def read_latest(self) -> ContentFrame | None:
        """Return the newest complete frame, or None if none/torn/already read."""
        (seq_before,) = struct.unpack_from("<Q", self._buf, _SEQ_OFFSET)
        if seq_before == 0 or seq_before & 1 or seq_before == self._last_seq:
            return None
        width, height, stride = struct.unpack_from("<III", self._buf, _DIMS_OFFSET)
        data = bytes(self._buf[_HEADER_SIZE:_HEADER_SIZE + self._payload])
        (seq_after,) = struct.unpack_from("<Q", self._buf, _SEQ_OFFSET)
        if seq_before != seq_after:
            return None  # the writer updated mid-copy; skip this frame
        self._last_seq = seq_before
        return ContentFrame(data=data, width=int(width), height=int(height), stride=int(stride))

    def close(self) -> None:
        buf, self._buf = getattr(self, "_buf", None), None
        if buf is not None:
            buf.release()
        self._shm.close()
