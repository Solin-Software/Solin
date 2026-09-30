"""Title + cover-art extraction for libobs-decoded media, via ffprobe/ffmpeg.

libobs is the only media engine, so ``QMediaPlayer`` no longer loads playing files
and its ``metaDataChanged`` never fires. This reads the title tag + embedded cover
art out of band with the ``ffprobe``/``ffmpeg`` CLIs (see
:mod:`solin.core.media.ffprobe_metadata`) on a worker thread, and delivers the
result as a ``QPixmap`` built on the GUI thread.

Only the embedded cover image is used — never a decoded frame — matching the
metadata the projection bar showed before the migration.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from solin.core.media import ffprobe_metadata

log = logging.getLogger(__name__)

# (path) -> (title, cover_image_bytes | None). Injectable for tests.
MetadataFn = Callable[[str], "tuple[str, bytes | None]"]
# Runs the extraction callable; default spawns a daemon thread, tests pass a
# synchronous runner.
Runner = Callable[[Callable[[], None]], None]


def _default_metadata_fn(path: str) -> tuple[str, bytes | None]:
    tags = ffprobe_metadata.probe_tags(path)
    cover = ffprobe_metadata.extract_cover(path, tags.cover_stream_index)
    return tags.title, cover


def _default_runner(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="solin-routed-metadata", daemon=True).start()


class RoutedMediaMetadataExtractor(QObject):
    """Reads title + cover for one media file at a time via ffprobe/ffmpeg.

    :meth:`request` supersedes any in-flight read; the result carries the caller's
    ``session_id`` so a late result for a superseded playback can be dropped.
    """

    # (session_id, title, cover) — cover is a QPixmap or None.
    metadata_ready = Signal(int, str, object)
    # Internal: (generation, session_id, title, cover_bytes|None) hopped from the
    # worker thread to the GUI thread (queued) so the QPixmap is built there.
    _result_ready = Signal(int, int, str, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        metadata_fn: MetadataFn | None = None,
        runner: Runner | None = None,
    ) -> None:
        super().__init__(parent)
        self._metadata_fn = metadata_fn or _default_metadata_fn
        self._runner = runner or _default_runner
        self._generation = 0
        self._result_ready.connect(self._deliver)

    def request(self, session_id: int, path: str) -> None:
        """Begin reading metadata for ``path``, tagging the result ``session_id``."""
        self._generation += 1
        generation = self._generation
        if not path:
            self.metadata_ready.emit(session_id, "", None)
            return
        metadata_fn = self._metadata_fn

        def _work() -> None:
            title, cover_bytes = "", None
            try:
                title, cover_bytes = metadata_fn(path)
            except Exception:  # noqa: BLE001 - extraction boundary (subprocess/IO)
                log.debug("routed metadata read errored for %r", path, exc_info=True)
            # The C++ object may have been deleted (e.g. its parent went away)
            # while this daemon thread ran; emitting on a dead object would crash.
            if not shiboken6.isValid(self):
                return
            try:
                self._result_ready.emit(generation, session_id, title, cover_bytes)
            except RuntimeError:
                pass

        self._runner(_work)

    def cancel(self) -> None:
        """Drop any in-flight read without emitting (its result is superseded)."""
        self._generation += 1

    @Slot(int, int, str, object)
    def _deliver(self, generation: int, session_id: int, title: str, cover_bytes) -> None:
        if generation != self._generation:
            return  # a newer request (or cancel) superseded this read
        cover: QPixmap | None = None
        if cover_bytes:
            image = QImage.fromData(cover_bytes)
            if not image.isNull():
                cover = QPixmap.fromImage(image)
        self.metadata_ready.emit(session_id, title, cover)


__all__ = ["RoutedMediaMetadataExtractor"]
