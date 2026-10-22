"""Shared-memory ring of fixed-size slots, for program media headed to the meeting publisher.

The single-slot :mod:`solin.core.scenes.content_frame_channel` keeps only the newest frame,
which suits previews but loses audio. The meeting publisher needs every audio chunk and, as
far as it can keep up, every video frame, so this ring holds ``slot_count`` items:

* one writer (the libobs sidecar) and one reader (the meeting publisher process);
* the app **creates and owns** the block; both processes attach by name with
  ``track=False`` so neither unlinks it (see ``content_frame_channel._attach_shared_memory``);
* items are numbered; each slot carries a seqlock (odd while being written) plus the item
  number, so the reader takes items in order, skips torn slots, and — when it falls more
  than a ring behind — jumps forward and counts what it dropped instead of stalling.

Layout (little-endian):
    header (64 bytes): magic "SMR1" @0, slot_count u32 @4, payload_size u32 @8,
                       written u64 @16 (number of items committed so far)
    slot i at 64 + i * (32 + payload_size):
        seq u64 @0 (even = stable), item u64 @8, timestamp_ns u64 @16,
        length u32 @24, meta u32 @28, payload @32
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from multiprocessing import shared_memory

from solin.core.scenes.content_frame_channel import _attach_shared_memory

_MAGIC = b"SMR1"
_HEADER_SIZE = 64
_SLOT_HEADER_SIZE = 32
_WRITTEN_OFFSET = 16


@dataclass(frozen=True, slots=True)
class RingItem:
    number: int
    timestamp_ns: int
    meta: int
    payload: bytes


def block_size(slot_count: int, payload_size: int) -> int:
    return _HEADER_SIZE + slot_count * (_SLOT_HEADER_SIZE + payload_size)


class _Ring:
    def __init__(self, name: str | None, slot_count: int, payload_size: int, *, create: bool) -> None:
        if create:
            if slot_count < 2 or payload_size <= 0:
                raise ValueError("a ring needs at least two slots and a positive payload size")
            self._shm = shared_memory.SharedMemory(
                create=True, size=block_size(slot_count, payload_size), name=name
            )
            buf = self._buf_or_raise()
            struct.pack_into("<4sII", buf, 0, _MAGIC, slot_count, payload_size)
            struct.pack_into("<Q", buf, _WRITTEN_OFFSET, 0)
        else:
            if not name:
                raise ValueError("attaching to a ring requires its name")
            self._shm = _attach_shared_memory(name)
            buf = self._buf_or_raise()
            magic, slot_count, payload_size = struct.unpack_from("<4sII", buf, 0)
            if magic != _MAGIC:
                self.close()
                raise ValueError("not a Solin media ring")
        self.slot_count = slot_count
        self.payload_size = payload_size

    def _buf_or_raise(self) -> memoryview:
        buf = self._shm.buf
        if buf is None:
            raise ValueError("media ring is closed")
        return buf

    def _slot_offset(self, number: int) -> int:
        return _HEADER_SIZE + (number % self.slot_count) * (_SLOT_HEADER_SIZE + self.payload_size)

    @property
    def name(self) -> str:
        return self._shm.name

    def written(self) -> int:
        return struct.unpack_from("<Q", self._buf_or_raise(), _WRITTEN_OFFSET)[0]

    def close(self) -> None:
        self._shm.close()

    def unlink(self) -> None:
        self._shm.unlink()


class MediaRingWriter(_Ring):
    """Producer side. Creates the block when ``create=True`` (app tests), else attaches."""

    def __init__(self, name: str | None = None, slot_count: int = 0, payload_size: int = 0, *,
                 create: bool = False) -> None:
        super().__init__(name, slot_count, payload_size, create=create)
        self._next = self.written()

    def write(self, payload: bytes | bytearray | memoryview, *, timestamp_ns: int = 0, meta: int = 0) -> int:
        """Commit one item; returns its number. Payloads larger than a slot are rejected."""
        length = len(payload)
        if length > self.payload_size:
            raise ValueError(f"payload of {length} bytes exceeds the {self.payload_size}-byte slot")
        buf = self._buf_or_raise()
        number = self._next
        offset = self._slot_offset(number)
        (seq,) = struct.unpack_from("<Q", buf, offset)
        writing = seq + 1 if seq % 2 == 0 else seq + 2
        struct.pack_into("<Q", buf, offset, writing)
        struct.pack_into("<QQII", buf, offset + 8, number, timestamp_ns & (2**64 - 1), length, meta)
        start = offset + _SLOT_HEADER_SIZE
        buf[start:start + length] = payload
        struct.pack_into("<Q", buf, offset, writing + 1)
        self._next = number + 1
        struct.pack_into("<Q", buf, _WRITTEN_OFFSET, self._next)
        return number


class MediaRingReader(_Ring):
    """Consumer side. Owns (creates) the block when ``create=True``, as the app does."""

    def __init__(self, name: str | None = None, slot_count: int = 0, payload_size: int = 0, *,
                 create: bool = False, from_start: bool = False) -> None:
        super().__init__(name, slot_count, payload_size, create=create)
        # A late joiner starts at the newest item rather than replaying stale media.
        self._next = 0 if (create or from_start) else self.written()
        self.dropped = 0

    def read_next(self) -> RingItem | None:
        """The next item in order, or None when nothing new is committed yet."""
        buf = self._buf_or_raise()
        written = self.written()
        if written == 0 or self._next >= written:
            return None
        if written - self._next > self.slot_count - 1:
            # Fell behind: the oldest slots are being overwritten. Skip to what is safe.
            skip_to = written - (self.slot_count - 1)
            self.dropped += skip_to - self._next
            self._next = skip_to
        offset = self._slot_offset(self._next)
        (seq_before,) = struct.unpack_from("<Q", buf, offset)
        number, timestamp_ns, length, meta = struct.unpack_from("<QQII", buf, offset + 8)
        if seq_before % 2 or number != self._next or length > self.payload_size:
            if number > self._next and seq_before % 2 == 0:
                # Overwritten by a newer item between our check and read: count and move on.
                self.dropped += number - self._next
                self._next = number
            return None
        start = offset + _SLOT_HEADER_SIZE
        payload = bytes(buf[start:start + length])
        (seq_after,) = struct.unpack_from("<Q", buf, offset)
        if seq_after != seq_before:
            return None  # rewritten while copying; try again on the next call
        self._next += 1
        return RingItem(number=number, timestamp_ns=timestamp_ns, meta=meta, payload=payload)
