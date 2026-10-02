"""Cross-platform shared-memory BGRA frame channel.

Used both directions of the libobs sidecar boundary:

* **content ingress** — the app renders content that cannot be an
  ``ffmpeg_source`` (yeartext, timers, the live browser, framed images) to BGRA
  and the sidecar composites it (app = writer/creator, sidecar = reader).
* **preview egress** — the sidecar renders a scene composite back for the
  editor preview (sidecar = writer, app = reader/creator).

Either side may **create** the block; the other **attaches** to it by name.
main's transport is Windows-only shared memory + D3D11; this is the
cross-platform equivalent on :class:`multiprocessing.shared_memory.SharedMemory`
(POSIX shm on Linux/macOS, a named file mapping on Windows).

One writer and one reader share a fixed-size block: a small header + a BGRA
payload. A seqlock (an even/odd sequence counter) lets the reader take a
tear-free snapshot without locking the writer:

    writer: seq→odd (writing) → write dims+payload → seq→even (done)
    reader: read seq → copy payload → read seq again; keep it only if both reads
            are equal and even (otherwise the writer was mid-frame; skip).

The reader also skips frames it has already delivered, so the consumer only
pushes genuinely new frames.
"""

from __future__ import annotations

import logging
import struct
import sys
from dataclasses import dataclass
from multiprocessing import shared_memory

log = logging.getLogger(__name__)

_MAGIC = b"SFC2"
_HEADER_SIZE = 64
# Header layout (little-endian): magic@0 (4s), seq@8 (uint64), width@16 (uint32),
# height@20 (uint32), stride@24 (uint32), format@28 (uint32; 0 = BGRA),
# media_epoch@32 (uint64). Identity and pixels are published in one snapshot.
_SEQ_OFFSET = 8
_DIMS_OFFSET = 16
_FORMAT_OFFSET = 28
_FORMAT_BGRA = 0
_EPOCH_OFFSET = 32


@dataclass(frozen=True, slots=True)
class ContentFrame:
    """One BGRA snapshot read from the channel."""

    data: bytes
    width: int
    height: int
    stride: int
    media_epoch: int


def _payload_bytes(width: int, height: int) -> int:
    return width * height * 4


def _attach_shared_memory(name: str) -> shared_memory.SharedMemory:
    """Attach to an existing block WITHOUT tracking it for unlink.

    Only the process that *created* the block should ever ``shm_unlink`` it.
    But on POSIX, :class:`multiprocessing.shared_memory.SharedMemory` registers
    every opened name with this process's ``resource_tracker`` — including plain
    attaches — and ``close()`` never unregisters, so an attaching process (here
    the sidecar) would unlink the *creator's* still-live block when it exits or
    restarts, breaking the channel. Opening with ``track=False`` (Python 3.13+),
    or unregistering after the fact (earlier versions), keeps ownership with the
    creator.
    """
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, track=False)
    shm = shared_memory.SharedMemory(name=name)
    if not sys.platform.startswith("win"):
        try:
            from multiprocessing import resource_tracker

            resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best-effort; a missing tracker is fine
            log.debug("resource_tracker unregister skipped", exc_info=True)
    return shm


def _init_header(buf: memoryview[int], width: int, height: int) -> None:
    struct.pack_into("<4s", buf, 0, _MAGIC)
    struct.pack_into("<Q", buf, _SEQ_OFFSET, 0)
    struct.pack_into("<III", buf, _DIMS_OFFSET, width, height, width * 4)
    struct.pack_into("<I", buf, _FORMAT_OFFSET, _FORMAT_BGRA)
    struct.pack_into("<Q", buf, _EPOCH_OFFSET, 0)


class SharedFrameChannelWriter:
    """Producer side: writes frames into the shared block.

    Creates the block by default (content ingress, app-side). Pass
    ``create=False`` to *attach* to a block another process created (preview
    egress, sidecar-side) — then the creator has already written the header.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        name: str | None = None,
        create: bool = True,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        self._width = width
        self._height = height
        self._payload = _payload_bytes(width, height)
        if create:
            self._shm = shared_memory.SharedMemory(
                create=True, size=_HEADER_SIZE + self._payload, name=name
            )
        else:
            if not name:
                raise ValueError("attaching a writer requires the block name")
            self._shm = _attach_shared_memory(name)  # creator owns the unlink
        buf = self._shm.buf
        if buf is None:
            raise ValueError("frame channel is closed")
        self._buf: memoryview[int] | None = buf
        self._version = 0
        if create:
            _init_header(buf, width, height)

    @property
    def name(self) -> str:
        return self._shm.name

    def write(
        self,
        data: bytes,
        *,
        stride: int | None = None,
        width: int | None = None,
        height: int | None = None,
        media_epoch: int = 0,
    ) -> None:
        """Publish one BGRA frame (tear-free via the seqlock).

        ``width``/``height`` describe *this frame*, which may be smaller than the
        channel — the content ingress publishes a picture at its own size and lets
        the scene item scale it up. Stamping the channel's dimensions instead told
        the reader that a small picture was a full-canvas frame, so it rendered at
        native size in the canvas corner with the rest left empty. Both default to
        the channel's dimensions, which is what the full-frame egress writers send.
        """
        buf = self._buf
        if buf is None:
            raise ValueError("frame channel is closed")
        if isinstance(media_epoch, bool) or not isinstance(media_epoch, int) or not 0 <= media_epoch < 2**64:
            raise ValueError("media epoch must be a non-negative 64-bit integer")
        frame_width = self._width if width is None else int(width)
        frame_height = self._height if height is None else int(height)
        stride = frame_width * 4 if stride is None else int(stride)
        if frame_width <= 0 or frame_height <= 0 or stride <= 0:
            return
        # Never promise the reader more rows than the block can hold: it trusts the
        # header and would read past the frame it was given.
        frame_height = min(frame_height, self._payload // stride)
        if frame_height <= 0:
            return
        count = min(len(data), frame_height * stride)
        writing = self._version + 1  # odd → a write is in progress
        struct.pack_into("<Q", buf, _SEQ_OFFSET, writing)
        struct.pack_into(
            "<III", buf, _DIMS_OFFSET, frame_width, frame_height, stride
        )
        struct.pack_into("<Q", buf, _EPOCH_OFFSET, media_epoch)
        buf[_HEADER_SIZE:_HEADER_SIZE + count] = data[:count]
        done = writing + 1  # even → complete
        struct.pack_into("<Q", buf, _SEQ_OFFSET, done)
        self._version = done

    def close(self) -> None:
        buf, self._buf = self._buf, None
        if buf is not None:
            buf.release()
        self._shm.close()

    def unlink(self) -> None:
        self._shm.unlink()


class SharedFrameChannelReader:
    """Consumer side: reads frames from the shared block.

    Attaches to an existing block by default (content ingress, sidecar-side).
    Pass ``create=True`` (with ``name=None`` for an auto-generated name) to
    *create and own* the block instead — the egress case, where the app-side
    reader owns the channel and the sidecar attaches as a writer.
    """

    def __init__(
        self,
        name: str | None,
        width: int,
        height: int,
        *,
        create: bool = False,
    ) -> None:
        self._payload = _payload_bytes(width, height)
        if create:
            self._shm = shared_memory.SharedMemory(
                create=True, size=_HEADER_SIZE + self._payload, name=name
            )
        else:
            if not name:
                raise ValueError("attaching a reader requires the block name")
            self._shm = _attach_shared_memory(name)  # creator owns the unlink
        buf = self._shm.buf
        if buf is None:
            raise ValueError("frame channel is closed")
        self._buf: memoryview[int] | None = buf
        if create:
            _init_header(buf, width, height)
        self._width = width
        self._height = height
        self._last_seq = 0
        if bytes(buf[0:4]) != _MAGIC:
            self.close()
            raise ValueError("not a Solin content frame channel")

    @property
    def name(self) -> str:
        return self._shm.name

    def unlink(self) -> None:
        self._shm.unlink()

    def read_latest(self) -> ContentFrame | None:
        """Return the newest complete frame, or None if none/torn/already read."""
        buf = self._buf
        if buf is None:
            raise ValueError("frame channel is closed")
        (seq_before,) = struct.unpack_from("<Q", buf, _SEQ_OFFSET)
        if seq_before == 0 or seq_before & 1 or seq_before == self._last_seq:
            return None
        width, height, stride = struct.unpack_from("<III", buf, _DIMS_OFFSET)
        (media_epoch,) = struct.unpack_from("<Q", buf, _EPOCH_OFFSET)
        data = bytes(buf[_HEADER_SIZE:_HEADER_SIZE + self._payload])
        (seq_after,) = struct.unpack_from("<Q", buf, _SEQ_OFFSET)
        if seq_before != seq_after:
            return None  # the writer updated mid-copy; skip this frame
        self._last_seq = seq_before
        return ContentFrame(
            data=data, width=int(width), height=int(height), stride=int(stride),
            media_epoch=media_epoch,
        )

    def close(self) -> None:
        buf, self._buf = self._buf, None
        if buf is not None:
            buf.release()
        self._shm.close()
