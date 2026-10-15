"""Title and embedded cover extraction with delivery on the Qt thread."""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable
from typing import Protocol

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QPixmap

from solin.core.foundation.thread_workers import CancellationFlag, ThreadedWorkerPool
from solin.core.media.media_extract import (
    ImagePolicy,
    MediaExtractCancelled,
    MediaExtractError,
    MediaExtractRequest,
    MediaExtractResult,
    get_media_extract_service,
)
from solin.ui.thumbnail_images import media_extract_image_to_qimage

log = logging.getLogger(__name__)


class MetadataFn(Protocol):
    def __call__(
        self,
        request: MediaExtractRequest,
        *,
        cancel: CancellationFlag,
    ) -> MediaExtractResult: ...


Runner = Callable[[Callable[[], None]], None]


def _default_metadata_fn(
    request: MediaExtractRequest,
    *,
    cancel: CancellationFlag,
) -> MediaExtractResult:
    return get_media_extract_service().extract(request, cancel=cancel)


class _ExtractionLifetime:
    """Cancel work on QObject destruction without retaining the QObject."""

    def __init__(self, workers: ThreadedWorkerPool | None) -> None:
        self.workers = workers
        self.cancellation: CancellationFlag | None = None

    def cancel(self) -> None:
        if self.cancellation is not None:
            self.cancellation.set()
            self.cancellation = None

    def close(self) -> None:
        self.cancel()
        if self.workers is not None:
            # Cancellation stops the native child. Do not block Qt destruction
            # while the worker reaps it; the pool retains work until it exits.
            self.workers.shutdown(timeout=0)


class RoutedMediaMetadataExtractor(QObject):
    """Read title and embedded cover for the latest playback request."""

    # (session_id, title, cover) — cover is a QPixmap or None.
    metadata_ready = Signal(int, str, object)
    _result_ready = Signal(int, int, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        metadata_fn: MetadataFn | None = None,
        runner: Runner | None = None,
    ) -> None:
        super().__init__(parent)
        self._metadata_fn = metadata_fn or _default_metadata_fn
        self._runner = runner
        self._lifetime = _ExtractionLifetime(ThreadedWorkerPool() if runner is None else None)
        self._generation = 0
        self._result_ready.connect(self._deliver)
        self.destroyed.connect(self._lifetime.close)
        # PySide can disconnect Python callbacks before deleting an unparented
        # wrapper. The finalizer owns only the lifetime state, never this QObject.
        weakref.finalize(self, self._lifetime.close)

    def request(self, session_id: int, path: str) -> None:
        """Supersede the previous extraction with a cancellable cover-only read."""
        self.cancel()
        generation = self._generation
        if not path:
            self.metadata_ready.emit(session_id, "", None)
            return
        try:
            request = MediaExtractRequest(
                source=path,
                image_policy=ImagePolicy.COVER_ONLY,
                require_title=True,
                require_duration=False,
            )
        except ValueError:
            log.debug("Invalid routed metadata source %r", path, exc_info=True)
            self.metadata_ready.emit(session_id, "", None)
            return
        cancellation = CancellationFlag()
        self._lifetime.cancellation = cancellation
        metadata_fn = self._metadata_fn
        owner_ref = weakref.ref(self)

        def _work() -> None:
            if cancellation.is_set():
                return
            result = None
            try:
                result = metadata_fn(request, cancel=cancellation)
            except MediaExtractCancelled:
                return
            except MediaExtractError as error:
                log.debug("Routed metadata read failed for %r: %s", path, error.failure)
            except Exception:  # noqa: BLE001 - injected extraction/IO boundary
                log.debug("routed metadata read errored for %r", path, exc_info=True)

            def _emit_result() -> None:
                owner = owner_ref()
                if owner is None or not shiboken6.isValid(owner):
                    return
                try:
                    owner._result_ready.emit(generation, session_id, result)
                except RuntimeError:
                    # Destruction can win between isValid() and signal emission.
                    log.debug("Routed metadata owner was destroyed before delivery")

            cancellation.run_if_active(_emit_result)

        if self._runner is not None:
            self._runner(_work)
        else:
            workers = self._lifetime.workers
            assert workers is not None
            workers.submit("solin-routed-metadata", _work)

    def cancel(self) -> None:
        """Cancel the active extraction and invalidate any queued result."""
        self._generation += 1
        self._lifetime.cancel()

    @Slot(int, int, object)
    def _deliver(
        self,
        generation: int,
        session_id: int,
        result: MediaExtractResult | None,
    ) -> None:
        if generation != self._generation:
            return  # a newer request (or cancel) superseded this read
        title = result.title if result is not None else ""
        cover: QPixmap | None = None
        if result is not None and result.image is not None and result.image.kind == "cover":
            try:
                image = media_extract_image_to_qimage(result.image)
            except ValueError:
                log.debug("Invalid routed metadata cover", exc_info=True)
            else:
                cover = QPixmap.fromImage(image)
                if cover.isNull():
                    cover = None
        self.metadata_ready.emit(session_id, title, cover)


__all__ = ["RoutedMediaMetadataExtractor"]
