"""
IdleMediaSource — a single, shared source for the custom idle screen.

The idle screen (custom image or video) is shown simultaneously on every
projection surface: each secondary-monitor :class:`ProjectionWindow` and the
on-screen :class:`FloatingPreviewWindow`. It decodes/loads **once** and hands the
resulting :class:`QImage` to every surface to paint (implicitly shared, painted on
the GUI thread), so the surfaces share a single frame buffer.

libobs is the only media engine and QtMultimedia has been removed, so this no
longer runs a ``QMediaPlayer``:

- an **image** loads directly into a :class:`QImage`;
- a **video** is represented by a single poster frame extracted with ffmpeg
  (``ffprobe``/``ffmpeg``) — animated idle video is not decoded here. Full looping
  idle video belongs in the sidecar's idle scene (a follow-up).

Lifecycle
─────────
- ``set_media(path)``  → load an image / extract a video poster (emit one frame).
- ``set_playing(bool)``→ no-op (there is no live decoder to gate).
- ``clear()``         → forget the current media.
- ``cleanup()``       → release resources before shutdown.
- ``current_image``   → the most recent frame, so a surface created *after* the
  idle began can paint the correct frame immediately.
"""

from __future__ import annotations

import logging
import threading

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from ..core.media import ffprobe_metadata
from ..core.media.formats import media_type_from_path

log = logging.getLogger(__name__)

_TARGET_FORMAT = QImage.Format.Format_ARGB32_Premultiplied
_POSTER_AT_MS = 1000


class IdleMediaSource(QObject):
    """Single image/poster source + frame fan-out for the custom idle screen."""

    #: A new frame is ready for every idle surface to paint.
    frame_ready = Signal(QImage)
    #: Internal: (generation, image_bytes|None) hopped from the poster worker.
    _poster_ready = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path: str = ""
        self._type: str = ""              # 'image' | 'video' | ''
        self._image: QImage | None = None  # last/static frame for late joiners
        self._generation = 0
        self._poster_ready.connect(self._on_poster_ready)

    # ── Read-only state ────────────────────────────────────────────────────

    @property
    def media_type(self) -> str:
        """'image', 'video' or '' (nothing loaded)."""
        return self._type

    @property
    def current_image(self) -> QImage | None:
        """Most recent frame, or None — for immediate paint on new surfaces."""
        return self._image

    # ── Public API ─────────────────────────────────────────────────────────

    def set_media(self, path: str) -> None:
        """Load an image or extract a video poster (emit one frame)."""
        self._generation += 1
        self._path = path
        self._type = media_type_from_path(path)
        self._image = None

        if self._type == "image":
            img = QImage(path)
            if img.isNull():
                self._type = ""
                return
            if img.format() != _TARGET_FORMAT:
                img = img.convertToFormat(_TARGET_FORMAT)
            self._image = img
            self.frame_ready.emit(img)
        elif self._type == "video":
            self._extract_poster(path, self._generation)

    def set_playing(self, playing: bool) -> None:
        """No-op — there is no live idle decoder (poster/image only)."""
        del playing

    def clear(self) -> None:
        """Forget the current media."""
        self._generation += 1
        self._path = ""
        self._type = ""
        self._image = None

    def cleanup(self) -> None:
        """Release resources — call before application shutdown."""
        self._generation += 1

    # ── Poster extraction (ffmpeg, off the GUI thread) ─────────────────────

    def _extract_poster(self, path: str, generation: int) -> None:
        def _work() -> None:
            image_bytes = None
            try:
                tags = ffprobe_metadata.probe_tags(path)
                at_ms = min(_POSTER_AT_MS, max(0, tags.duration_ms // 10))
                image_bytes = ffprobe_metadata.extract_thumbnail(path, at_ms)
            except Exception:  # noqa: BLE001 - poster extraction boundary
                log.debug("idle poster extraction failed for %s", path, exc_info=True)
            if not shiboken6.isValid(self):
                return  # idle source deleted before the worker finished
            try:
                self._poster_ready.emit(generation, image_bytes)
            except RuntimeError:
                pass

        threading.Thread(target=_work, name="solin-idle-poster", daemon=True).start()

    @Slot(int, object)
    def _on_poster_ready(self, generation: int, image_bytes) -> None:
        if generation != self._generation or not image_bytes:
            return
        img = QImage()
        img.loadFromData(image_bytes)
        if img.isNull():
            return
        if img.format() != _TARGET_FORMAT:
            img = img.convertToFormat(_TARGET_FORMAT)
        self._image = img
        self.frame_ready.emit(img)
