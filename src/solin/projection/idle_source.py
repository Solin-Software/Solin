"""
IdleMediaSource — a single, shared decoder for the custom idle screen.

Why this exists
───────────────
The idle screen (custom image or looping video) is shown simultaneously on
every projection surface: each secondary-monitor :class:`ProjectionWindow` and
the on-screen :class:`FloatingPreviewWindow`.

Historically every surface owned its *own* ``QMediaPlayer`` and decoded the
same idle video independently.  With N surfaces that meant N independent
decoders of the same file, which caused two concrete bugs:

  1. **De-sync** — each player started/looped on its own clock, so the monitors
     drifted out of step (unlike normal video projection, which is frame-locked
     because it shares one decoder).
  2. **CPU / memory blow-up** — N simultaneous decoders + N format conversions
     per frame pushed CPU past 10 % and intermittently exhausted image memory
     (``QImage: out of memory``).

This class fixes both at the root by mirroring the normal-video pipeline
(:class:`solin.core.media.playback.MediaController` → ``distribute_frame``): decode
**once**, convert **once**, then hand the resulting :class:`QImage` to every
surface to paint.  Because :class:`QImage` is implicitly shared and all painting
happens on the GUI thread, the N surfaces share a single frame buffer.

Lifecycle
─────────
- ``set_media(path)``  → load an image (emit one frame) or start a looping,
  muted video (emit a frame per decoded frame).
- ``clear()``         → stop and go idle.
- ``cleanup()``       → release the player before shutdown.
- ``current_image``   → the most recent frame, so a surface created *after*
  playback began (hot-plugged monitor, floating-window respawn) can paint the
  correct frame immediately instead of flashing black until the next frame.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame

from ..core.media.formats import media_type_from_path

_TARGET_FORMAT = QImage.Format.Format_ARGB32_Premultiplied

#: When True, an idle video that was paused (because nothing was showing it)
#: restarts from the beginning the next time it becomes visible — every
#: appearance of the idle screen begins the loop fresh, which looks more
#: deliberate.  When False the video simply resumes from where it was paused.
IDLE_VIDEO_RESTART_ON_RESUME = True


class IdleMediaSource(QObject):
    """Single decoder + frame fan-out for the custom idle screen.

    Emits :attr:`frame_ready` with a ready-to-paint :class:`QImage` — once for a
    static image, and continuously for a looping video.
    """

    #: A new frame is ready for every idle surface to paint.
    frame_ready = Signal(QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._path: str = ""
        self._type: str = ""              # 'image' | 'video' | ''
        self._image: QImage | None = None  # last/static frame for late joiners

        # Idle media is always silent.
        self._audio_output = QAudioOutput()
        self._audio_output.setVolume(0.0)

        self._video_sink = QVideoSink(self)
        self._video_sink.videoFrameChanged.connect(self._on_video_frame)

        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoSink(self._video_sink)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)

    # ── Read-only state ────────────────────────────────────────────────────

    @property
    def media_type(self) -> str:
        """'image', 'video' or '' (nothing loaded)."""
        return self._type

    @property
    def current_image(self) -> QImage | None:
        """Most recent decoded frame, or None — for immediate paint on new surfaces."""
        return self._image

    # ── Public API ─────────────────────────────────────────────────────────

    def set_media(self, path: str) -> None:
        """Load an image (emit one frame) or start a looping muted video."""
        self._stop_player()
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
            self._player.setSource(QUrl.fromLocalFile(path))
            # Playback is started by the owner via set_playing() according to
            # whether any surface is actually showing the idle screen — so the
            # decoder stays paused (zero CPU) while a clip/image/timer plays.

    def set_playing(self, playing: bool) -> None:
        """Play or pause the shared video decoder.

        No-op for images (there is no decoder to run).  The owner calls this as
        the idle screen becomes visible/hidden across the surfaces, so a custom
        idle video is decoded only while it is actually on screen somewhere.
        """
        if self._type != "video":
            return
        state = self._player.playbackState()
        if playing:
            if state != QMediaPlayer.PlaybackState.PlayingState:
                if IDLE_VIDEO_RESTART_ON_RESUME:
                    # Rewind so the loop starts fresh each time the idle reappears.
                    self._player.setPosition(0)
                self._player.play()
        elif state == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()

    def clear(self) -> None:
        """Stop any playback and forget the current media."""
        self._stop_player()
        self._path = ""
        self._type = ""
        self._image = None

    def cleanup(self) -> None:
        """Release media resources — call before application shutdown."""
        self._stop_player()

    # ── Video frame slot ───────────────────────────────────────────────────

    @Slot(QVideoFrame)
    def _on_video_frame(self, frame: QVideoFrame) -> None:
        """Convert a decoded video frame to a paint-ready QImage and fan it out.

        Conversion happens here exactly once; the same QImage is then shared by
        every idle surface (implicitly shared, read-only on the GUI thread).
        """
        if self._type != "video" or not frame.isValid():
            return
        img = frame.toImage()  # toImage() already returns a detached QImage
        if img.isNull():
            return
        if img.format() != _TARGET_FORMAT:
            img = img.convertToFormat(_TARGET_FORMAT)
        self._image = img
        self.frame_ready.emit(img)

    # ── Internals ──────────────────────────────────────────────────────────

    def _stop_player(self) -> None:
        if self._player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self._player.stop()
        self._player.setSource(QUrl())
