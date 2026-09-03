"""Title + cover-art extraction for engine-routed (libobs-decoded) local media.

When a local file is routed to the libobs sidecar (Fork A), ``QMediaPlayer`` is
not loaded, so its ``metaDataChanged`` never fires and the projection bar loses
the title tag + embedded cover art it shows for the Qt path. This reads that same
metadata out of band: a *metadata-only* ``QMediaPlayer`` (no audio output, no
video sink, never played) that opens the file just far enough for its container
tags to parse, reads them, and tears down.

Parity with :meth:`MediaController._on_metadata_changed` is deliberate: only the
embedded ``CoverArtImage``/``ThumbnailImage`` is used as cover art — never a
decoded frame — so routed and non-routed playback show the same thing.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QMediaMetaData, QMediaPlayer

log = logging.getLogger(__name__)


def read_playback_metadata(meta: QMediaMetaData) -> tuple[str, QPixmap | None]:
    """(title, embedded cover) from a ``QMediaMetaData``, matching the Qt path.

    Mirrors ``MediaController._on_metadata_changed``: a stripped ``Title`` tag and
    the first non-null ``CoverArtImage``/``ThumbnailImage`` as a ``QPixmap`` — no
    frame-grab fallback.
    """
    title = ""
    title_val = meta.value(QMediaMetaData.Key.Title)
    if isinstance(title_val, str) and title_val.strip():
        title = title_val.strip()
    for key in (QMediaMetaData.Key.CoverArtImage, QMediaMetaData.Key.ThumbnailImage):
        value = meta.value(key)
        if value is None:
            continue
        if isinstance(value, QImage) and not value.isNull():
            return title, QPixmap.fromImage(value)
        if isinstance(value, QPixmap) and not value.isNull():
            return title, value
    return title, None


class RoutedMediaMetadataExtractor(QObject):
    """Reads title + cover for one local file at a time via a metadata-only player.

    :meth:`request` supersedes any in-flight read; the result carries the caller's
    ``session_id`` so a late result for a superseded playback can be dropped.
    """

    # (session_id, title, cover) — cover is a QPixmap or None.
    metadata_ready = Signal(int, str, object)

    _TIMEOUT_MS = 8000
    # Grace after the media loads for a cover that arrives in a later
    # metaDataChanged (some backends attach the image tag after LoadedMedia).
    _COVER_GRACE_MS = 400

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        player_factory: Callable[[QObject], QMediaPlayer] | None = None,
    ) -> None:
        super().__init__(parent)
        self._player_factory = player_factory or (lambda p: QMediaPlayer(p))
        self._player: QMediaPlayer | None = None
        self._session_id = -1
        self._active = False
        self._reached_loaded = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._finish)
        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._finish)

    def request(self, session_id: int, path: str) -> None:
        """Begin reading metadata for ``path``, tagging the result ``session_id``."""
        self.cancel()
        self._session_id = session_id
        self._active = True
        self._reached_loaded = False
        player = self._player_factory(self)
        self._player = player
        # Metadata-only: no audio output, no video sink, never play() — just load
        # far enough for the container tags to parse.
        player.metaDataChanged.connect(self._on_metadata_changed)
        player.mediaStatusChanged.connect(self._on_status_changed)
        player.errorOccurred.connect(self._on_error)
        self._timeout.start(self._TIMEOUT_MS)
        player.setSource(QUrl.fromLocalFile(path))

    def cancel(self) -> None:
        """Drop any in-flight read without emitting."""
        self._timeout.stop()
        self._grace.stop()
        self._active = False
        self._session_id = -1
        self._reached_loaded = False
        player, self._player = self._player, None
        if player is not None:
            # Disconnect before deleteLater: the old player outlives this call
            # (deleteLater is async), and a status/error signal already in flight
            # for it must not drive the next request's state machine.
            for signal, slot in (
                (player.metaDataChanged, self._on_metadata_changed),
                (player.mediaStatusChanged, self._on_status_changed),
                (player.errorOccurred, self._on_error),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            try:
                player.stop()
                player.setSource(QUrl())
            except Exception:  # noqa: BLE001 - Qt teardown boundary
                log.debug("metadata extractor teardown errored", exc_info=True)
            player.deleteLater()

    # ── Qt signal handlers ─────────────────────────────────────────────────

    def _is_current_sender(self) -> bool:
        # A signal from a superseded player (deleteLater is async, so it lives on)
        # must not touch the current request's state.
        return self.sender() is self._player

    def _on_status_changed(self, status) -> None:
        if not self._is_current_sender():
            return
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)
        if status in loaded:
            self._reached_loaded = True
            self._settle()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish()

    def _on_metadata_changed(self) -> None:
        # Only act once the media has loaded; a cover arriving during the grace
        # window settles the read early.
        if self._is_current_sender() and self._reached_loaded:
            self._settle()

    def _on_error(self, *_args) -> None:
        if self._is_current_sender():
            self._finish()

    def _settle(self) -> None:
        """Finish now if a cover is available; otherwise wait out the grace window."""
        player = self._player
        if player is None or not self._active:
            return
        _, cover = read_playback_metadata(player.metaData())
        if cover is not None:
            self._finish()
        elif not self._grace.isActive():
            self._grace.start(self._COVER_GRACE_MS)

    def _finish(self) -> None:
        if not self._active:
            return
        player = self._player
        session_id = self._session_id
        title, cover = "", None
        if player is not None:
            try:
                title, cover = read_playback_metadata(player.metaData())
            except Exception:  # noqa: BLE001 - Qt metadata boundary
                log.debug("metadata read errored", exc_info=True)
        self.cancel()
        self.metadata_ready.emit(session_id, title, cover)
