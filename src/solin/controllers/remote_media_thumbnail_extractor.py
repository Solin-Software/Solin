"""Thread-safe bridge from remote HTTP requests to Qt media extraction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import threading
from typing import Any, Final

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..core.media.formats import MediaKind
from ..ui.thumbnail_images import pixmap_to_jpeg_bytes


log = logging.getLogger(__name__)

_MAX_THUMBNAIL_WIDTH: Final = 640
_MAX_THUMBNAIL_HEIGHT: Final = 360
_MAX_PENDING_MEDIA: Final = 32
_EXTRACTION_TIMEOUT_SECONDS: Final = {
    MediaKind.AUDIO: 4.0,
    MediaKind.IMAGE: 8.0,
    MediaKind.VIDEO: 12.0,
}


@dataclass(slots=True)
class _ExtractionRequest:
    location: str
    media_kind: MediaKind
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[bytes | None]
    cancelled: threading.Event


class RemoteMediaThumbnailExtractor(QObject):
    """Deduplicate and bound media thumbnail work owned by the Qt thread."""

    requested = Signal(object)
    cancellation_requested = Signal(object)

    def __init__(
        self,
        media_info_queue_factory: Callable[[QObject], Any],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._queue = media_info_queue_factory(self)
        self._closed = threading.Event()
        self._next_index = 0
        self._index_to_key: dict[int, tuple[str, MediaKind]] = {}
        self._pending: dict[tuple[str, MediaKind], list[_ExtractionRequest]] = {}
        self.requested.connect(self._dispatch)
        self.cancellation_requested.connect(self._cancel)
        self._queue.info_ready.connect(self._on_info_ready)

    async def extract(self, location: str, media_kind: MediaKind) -> bytes | None:
        """Return a bounded JPEG without touching Qt from the server thread."""

        if self._closed.is_set() or media_kind is MediaKind.UNKNOWN:
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes | None] = loop.create_future()
        request = _ExtractionRequest(
            location,
            media_kind,
            loop,
            future,
            threading.Event(),
        )
        self.requested.emit(request)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=_EXTRACTION_TIMEOUT_SECONDS[media_kind],
            )
        except TimeoutError:
            request.cancelled.set()
            self.cancellation_requested.emit(request)
            return None
        except asyncio.CancelledError:
            request.cancelled.set()
            self.cancellation_requested.emit(request)
            raise

    @Slot(object)
    def _dispatch(self, request: _ExtractionRequest) -> None:
        if request.cancelled.is_set():
            return
        if self._closed.is_set():
            self._complete(request, None)
            return
        key = self._source_key(request.location, request.media_kind)
        waiting = self._pending.get(key)
        if waiting is not None:
            waiting.append(request)
            return
        if len(self._pending) >= _MAX_PENDING_MEDIA:
            self._complete(request, None)
            return

        index = self._next_index
        self._next_index += 1
        self._pending[key] = [request]
        self._index_to_key[index] = key
        try:
            self._queue.request(index, request.location, request.media_kind.value)
        except Exception:  # noqa: BLE001 - Qt media adapter boundary
            log.exception("Could not schedule remote media thumbnail extraction")
            self._finish(index, None)

    @Slot(object)
    def _cancel(self, request: _ExtractionRequest) -> None:
        key = self._source_key(request.location, request.media_kind)
        waiting = self._pending.get(key)
        if waiting is None:
            return
        remaining = [current for current in waiting if current is not request]
        if remaining:
            self._pending[key] = remaining
            return
        self._pending.pop(key, None)
        index = next(
            (current for current, current_key in self._index_to_key.items() if current_key == key),
            None,
        )
        if index is None:
            return
        self._index_to_key.pop(index, None)
        try:
            self._queue.invalidate(index)
        except (AttributeError, RuntimeError):
            log.warning("Could not cancel remote media thumbnail extraction", exc_info=True)

    @Slot(int, object, str)
    def _on_info_ready(self, index: int, pixmap: Any, _title: str) -> None:
        data: bytes | None = None
        try:
            if pixmap is not None and not pixmap.isNull():
                if pixmap.width() > _MAX_THUMBNAIL_WIDTH or pixmap.height() > _MAX_THUMBNAIL_HEIGHT:
                    pixmap = pixmap.scaled(
                        _MAX_THUMBNAIL_WIDTH,
                        _MAX_THUMBNAIL_HEIGHT,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                data = pixmap_to_jpeg_bytes(pixmap)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            log.warning("Could not encode extracted remote media thumbnail", exc_info=True)
        self._finish(index, data)

    def shutdown(self) -> None:
        """Cancel owned Qt extraction and resolve all remote waiters once."""

        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.shutdown()
        except (AttributeError, RuntimeError):
            log.warning("Could not stop remote media thumbnail extraction", exc_info=True)
        pending = tuple(request for requests in self._pending.values() for request in requests)
        self._pending.clear()
        self._index_to_key.clear()
        for request in pending:
            self._complete(request, None)

    def _finish(self, index: int, data: bytes | None) -> None:
        key = self._index_to_key.pop(index, None)
        if key is None:
            return
        requests = self._pending.pop(key, ())
        for request in requests:
            self._complete(request, data)

    @staticmethod
    def _complete(request: _ExtractionRequest, data: bytes | None) -> None:
        if request.cancelled.is_set():
            return

        def complete() -> None:
            if not request.future.done():
                request.future.set_result(data)

        request.loop.call_soon_threadsafe(complete)

    @staticmethod
    def _source_key(location: str, media_kind: MediaKind) -> tuple[str, MediaKind]:
        return os.path.normcase(os.path.abspath(location)), media_kind
