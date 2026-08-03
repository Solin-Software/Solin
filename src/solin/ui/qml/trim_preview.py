"""QML-facing trim preview: a ``QMediaPlayer``-shaped object backed by libobs.

``MediaTrimDialog.qml`` used a QtMultimedia ``MediaPlayer`` + ``VideoOutput``.
This exposes the same surface — the subset the dialog actually touches — as a
context property, and delivers frames through a ``QQuickImageProvider``, which is
how every other Python→QML image in Solin already works (see
``ui/qml/playlist/visuals.py`` and ``ui/qml/svg_icons.py``). No ``qmlRegisterType``
is introduced: the repo has none, and a context property + image provider is the
established idiom.

Two details are load-bearing for the QML side:

* ``playbackState`` / ``mediaStatus`` are ``Property(int)``. QML compares them
  with ``===`` against integer literals, and a QVariant-wrapped Python
  ``IntEnum`` fails strict equality.
* Every change signal carries its new value, because the dialog's handlers take
  it as an argument (``onDurationChanged: function(duration)``). A signal with no
  argument would hand QML ``undefined``, and ``undefined <= 0`` is false — the
  dialog would store ``undefined`` as the duration and break.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from solin.core.media.obs_preview import ObsPreviewPlayer

log = logging.getLogger(__name__)

#: QML image-provider name; URLs look like ``image://trimpreview/<revision>``.
PROVIDER_ID = "trimpreview"


class TrimPreviewImageProvider(QQuickImageProvider):
    """Hands the latest preview frame to QML.

    The revision in the URL is pure cache-busting — the same trick
    ``PlaylistThumbnailProvider`` uses — so QML re-requests the image whenever
    the frame counter changes.
    """

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._image = QImage()

    def set_image(self, image: QImage) -> None:
        self._image = image

    def requestImage(self, image_id, size, requested_size):  # noqa: N802 - Qt API
        image = self._image
        if image.isNull():
            return QImage()
        if requested_size is not None and requested_size.isValid():
            width = requested_size.width()
            height = requested_size.height()
            if width > 0 and height > 0:
                return image.scaled(width, height)
        return image


class TrimPreviewPlayer(QObject):
    """``MediaPlayer``-shaped facade over :class:`ObsPreviewPlayer` for QML."""

    sourceChanged = Signal(str)
    durationChanged = Signal(int)
    positionChanged = Signal(int)
    playbackStateChanged = Signal(int)
    mediaStatusChanged = Signal(int)
    seekableChanged = Signal(bool)
    errorOccurred = Signal(int, str)
    #: Bumped per frame; QML binds the preview Image's source to it.
    frameRevisionChanged = Signal(int)
    mutedChanged = Signal(bool)
    volumeChanged = Signal(float)

    def __init__(self, provider: TrimPreviewImageProvider, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._engine = ObsPreviewPlayer(self)
        self._source = ""
        self._revision = 0

        self._engine.duration_changed.connect(self.durationChanged)
        self._engine.position_changed.connect(self.positionChanged)
        self._engine.playback_state_changed.connect(self.playbackStateChanged)
        self._engine.media_status_changed.connect(self.mediaStatusChanged)
        self._engine.seekable_changed.connect(self.seekableChanged)
        self._engine.error_occurred.connect(self._on_error)
        self._engine.frame_ready.connect(self._on_frame)

    # ── Frames ────────────────────────────────────────────────────────────

    def _on_frame(self, image: QImage) -> None:
        self._provider.set_image(image)
        self._revision += 1
        self.frameRevisionChanged.emit(self._revision)

    def _on_error(self, message: str) -> None:
        # The dialog shows errorString verbatim to the operator; the numeric code
        # is accepted for signature parity and discarded there.
        self.errorOccurred.emit(0, message)

    # ── Properties ────────────────────────────────────────────────────────

    def _get_source(self) -> str:
        return self._source

    def _set_source(self, url: str) -> None:
        url = url or ""
        if url == self._source:
            return
        self._source = url
        self._revision += 1
        self.frameRevisionChanged.emit(self._revision)
        self._engine.set_source(url)
        self.sourceChanged.emit(url)

    source = Property(str, _get_source, _set_source, notify=sourceChanged)

    def _get_position(self) -> int:
        return int(self._engine.position)

    def _set_position(self, ms: int) -> None:
        self._engine.seek(int(ms))

    position = Property(int, _get_position, _set_position, notify=positionChanged)

    def _get_duration(self) -> int:
        return int(self._engine.duration)

    duration = Property(int, _get_duration, notify=durationChanged)

    def _get_playback_state(self) -> int:
        return int(self._engine.playback_state)

    playbackState = Property(int, _get_playback_state, notify=playbackStateChanged)

    def _get_media_status(self) -> int:
        return int(self._engine.media_status)

    mediaStatus = Property(int, _get_media_status, notify=mediaStatusChanged)

    def _get_seekable(self) -> bool:
        return bool(self._engine.seekable)

    seekable = Property(bool, _get_seekable, notify=seekableChanged)

    def _get_revision(self) -> int:
        return self._revision

    frameRevision = Property(int, _get_revision, notify=frameRevisionChanged)

    def _get_muted(self) -> bool:
        return self._engine.muted

    def _set_muted(self, muted: bool) -> None:
        if bool(muted) == self._engine.muted:
            return
        self._engine.set_muted(bool(muted))
        self.mutedChanged.emit(bool(muted))

    muted = Property(bool, _get_muted, _set_muted, notify=mutedChanged)

    def _get_volume(self) -> float:
        return float(self._engine.volume)

    def _set_volume(self, volume: float) -> None:
        self._engine.set_volume(float(volume))
        self.volumeChanged.emit(float(volume))

    volume = Property(float, _get_volume, _set_volume, notify=volumeChanged)

    # ── Methods QML calls ─────────────────────────────────────────────────

    @Slot()
    def play(self) -> None:
        self._engine.play()

    @Slot()
    def pause(self) -> None:
        self._engine.pause()

    @Slot()
    def stop(self) -> None:
        self._engine.stop()

    @Slot()
    def shutdown(self) -> None:
        self._engine.shutdown()


def install_trim_preview(context_properties: dict, image_providers: dict) -> TrimPreviewPlayer:
    """Register the preview player + its provider into a QML host's config.

    Returns the player so the caller can keep a reference (and shut it down);
    QML sees it as ``trimPreview``.
    """
    provider = TrimPreviewImageProvider()
    player = TrimPreviewPlayer(provider)
    image_providers[PROVIDER_ID] = provider
    context_properties["trimPreview"] = player
    return player


__all__ = [
    "PROVIDER_ID",
    "TrimPreviewImageProvider",
    "TrimPreviewPlayer",
    "install_trim_preview",
]
