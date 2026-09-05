"""App side of the scene-card live thumbnails.

Owns one shared-memory block holding a vertical atlas of small scene renders —
row ``i`` is the scene at index ``i`` of the id list handed to the engine — reads
it on a worker thread and emits one image per scene.

One block rather than one per card keeps the engine plumbing to a single
descriptor, and the block is only allocated while something is actually watching
(the panel being open), so a closed panel costs nothing.
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
from solin.core.scenes.model import (
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
)

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 1.0 / 12.0


class SceneThumbnailEgressController(QObject):
    """Publishes a live thumbnail per scene from one shared atlas."""

    thumbnail_ready = Signal(str, QImage)  # (scene_id, image)

    def __init__(self, channel_id: str = "scene-thumbnails", parent=None) -> None:
        super().__init__(parent)
        self._channel_id = channel_id
        self._lock = threading.Lock()
        self._reader: SharedFrameChannelReader | None = None
        self._descriptor: FrameChannelDescriptor | None = None
        self._scene_ids: tuple[str, ...] = ()
        self._cell = (0, 0)
        self._generation = 0
        self._closed = False
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="solin-scene-thumbnails", daemon=True
        )
        self._worker.start()

    @property
    def descriptor(self) -> FrameChannelDescriptor | None:
        with self._lock:
            return self._descriptor

    @property
    def scene_ids(self) -> tuple[str, ...]:
        with self._lock:
            return self._scene_ids

    @property
    def cell_size(self) -> tuple[int, int]:
        with self._lock:
            return self._cell

    def reconfigure(
        self,
        scene_ids: tuple[str, ...],
        cell_width: int,
        cell_height: int,
    ) -> bool:
        """Size the atlas for these scenes. True when the descriptor changed."""
        scene_ids = tuple(scene_ids)
        cell_width, cell_height = max(1, int(cell_width)), max(1, int(cell_height))
        with self._lock:
            if self._closed:
                return False
            unchanged = (
                self._scene_ids == scene_ids
                and self._cell == (cell_width, cell_height)
                and self._reader is not None
            )
        if unchanged:
            return False
        if not scene_ids:
            self.stop()
            return True
        try:
            reader = SharedFrameChannelReader(
                None, cell_width, cell_height * len(scene_ids), create=True
            )
        except Exception:  # noqa: BLE001 - shared-memory boundary
            log.warning("could not create the thumbnail channel", exc_info=True)
            return False
        self._generation += 1
        descriptor = FrameChannelDescriptor(
            channel_id=self._channel_id,
            generation=self._generation,
            producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
            transport=FrameChannelTransport.SHARED_MEMORY_BGRA,
            handle_token=reader.name,
            width=cell_width,
            height=cell_height * len(scene_ids),
            pixel_format=VideoPixelFormat.BGRA,
            color_space=VideoColorSpace.SRGB,
            color_range=VideoColorRange.FULL,
        )
        with self._lock:
            old, self._reader = self._reader, reader
            self._descriptor = descriptor
            self._scene_ids = scene_ids
            self._cell = (cell_width, cell_height)
        self._release(old)
        return True

    def stop(self) -> None:
        """Drop the block; a closed panel should not pay for thumbnails."""
        with self._lock:
            old, self._reader = self._reader, None
            self._descriptor = None
            self._scene_ids = ()
            self._cell = (0, 0)
        self._release(old)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            reader, self._reader = self._reader, None
            self._descriptor = None
        self._stop.set()
        self._worker.join(timeout=2.0)
        self._release(reader)

    def _release(self, reader: SharedFrameChannelReader | None) -> None:
        if reader is None:
            return
        for step in (reader.close, reader.unlink):  # the app owns the block
            try:
                step()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("thumbnail channel teardown errored", exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(_POLL_INTERVAL_S):
            with self._lock:
                if self._closed:
                    return
                reader = self._reader
                scene_ids = self._scene_ids
                cell_width, cell_height = self._cell
            if reader is None or not scene_ids:
                continue
            try:
                frame = reader.read_latest()
            except Exception:  # noqa: BLE001 - shared-memory boundary
                log.warning("thumbnail read errored", exc_info=True)
                continue
            if frame is None:
                continue
            # BGRA reinterpreted as ARGB32, matching the preview egress path.
            atlas = QImage(
                frame.data, frame.width, frame.height, frame.stride,
                QImage.Format.Format_ARGB32,
            )
            if atlas.isNull():
                continue
            for index, scene_id in enumerate(scene_ids):
                top = index * cell_height
                if top + cell_height > atlas.height():
                    break
                # copy() so each image owns its pixels once the frame is dropped
                row = atlas.copy(0, top, cell_width, cell_height)
                if not row.isNull():
                    self.thumbnail_ready.emit(scene_id, row)
