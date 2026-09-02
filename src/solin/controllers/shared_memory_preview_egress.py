"""App-side cross-platform preview egress for the libobs engine.

The editor canvas shows a live composite of the scene being edited. main's
transport is a Windows-only shared-memory subscriber; this is the cross-platform
equivalent: the app **creates and owns** a ``SHARED_MEMORY_BGRA`` block, and the
libobs sidecar **attaches as a writer** and composites the edited scene into it
(see ``core/scenes/libobs_preview_egress.py``).

It mirrors :class:`SceneFrameEgressController`'s surface (``frame_ready`` /
``descriptor_changed`` / ``descriptor`` / ``reconfigure`` / ``close``) so
``main_window`` wires it identically; a worker thread polls the channel and
emits each new frame as a detached :class:`QImage`.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from solin.core.scenes.content_frame_channel import SharedFrameChannelReader
from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.model import VideoColorRange, VideoColorSpace, VideoPixelFormat

log = logging.getLogger(__name__)

_CHANNEL_ID = "solin-preview"
_POLL_INTERVAL_S = 1.0 / 30.0


class SharedMemoryPreviewEgressController(QObject):
    """Own a cross-platform BGRA channel the sidecar writes; emit QImages."""

    frame_ready = Signal(object)
    descriptor_changed = Signal(object)

    def __init__(self, width: int, height: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._reader: SharedFrameChannelReader | None = None
        self._descriptor: FrameChannelDescriptor | None = None
        self._generation = 0
        self._closed = False
        self._stop = threading.Event()
        self._configure(width, height)
        self._worker = threading.Thread(
            target=self._run, name="solin-preview-egress", daemon=True
        )
        self._worker.start()

    @property
    def descriptor(self) -> FrameChannelDescriptor | None:
        with self._lock:
            return self._descriptor

    def reconfigure(self, width: int, height: int) -> None:
        with self._lock:
            if self._closed:
                return
            current = self._descriptor
        if current is not None and current.width == int(width) and current.height == int(height):
            return
        self._generation += 1
        self._configure(width, height)
        self.descriptor_changed.emit(self.descriptor)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            reader = self._reader
            self._reader = None
            self._descriptor = None
        self._stop.set()
        self._worker.join(timeout=2.0)
        self._release(reader)

    # ── internals ──────────────────────────────────────────────────────────

    def _configure(self, width: int, height: int) -> None:
        width = max(1, int(width))
        height = max(1, int(height))
        try:
            reader = SharedFrameChannelReader(None, width, height, create=True)
        except Exception:  # noqa: BLE001 - shared-memory boundary
            log.warning("could not create the preview egress channel", exc_info=True)
            return
        descriptor = FrameChannelDescriptor(
            channel_id=_CHANNEL_ID,
            generation=self._generation,
            producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
            transport=FrameChannelTransport.SHARED_MEMORY_BGRA,
            handle_token=reader.name,
            width=width,
            height=height,
            pixel_format=VideoPixelFormat.BGRA,
            color_space=VideoColorSpace.SRGB,
            color_range=VideoColorRange.FULL,
        )
        with self._lock:
            old = self._reader
            self._reader = reader
            self._descriptor = descriptor
        self._release(old)

    def _release(self, reader: SharedFrameChannelReader | None) -> None:
        if reader is None:
            return
        try:
            reader.close()
        except Exception:  # noqa: BLE001 - best-effort
            log.debug("preview egress reader close errored", exc_info=True)
        try:
            reader.unlink()  # the app owns the block, so it also unlinks it
        except Exception:  # noqa: BLE001 - best-effort (may already be gone)
            log.debug("preview egress reader unlink errored", exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(_POLL_INTERVAL_S):
            with self._lock:
                if self._closed:
                    return
                reader = self._reader
            if reader is None:
                continue
            try:
                frame = reader.read_latest()
            except Exception:  # noqa: BLE001 - shared-memory boundary
                log.warning("preview egress read errored", exc_info=True)
                continue
            if frame is None:
                continue
            # BGRA reinterpreted as ARGB32 (matches the native egress path); copy
            # so the QImage owns its pixels once the transient frame is dropped.
            image = QImage(
                frame.data, frame.width, frame.height, frame.stride,
                QImage.Format.Format_ARGB32,
            ).copy()
            if not image.isNull():
                self.frame_ready.emit(image)
