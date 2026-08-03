"""Isolated libobs media preview — playback that never reaches the audience.

The trim dialog has to play a clip so the operator can pick start/end points,
while a meeting may be live on the projector. That rules out the shared playback
engine (:mod:`obs_playback`), which deliberately routes its source through the
projection *programme*: previewing a clip there would put it on the screen.

So this opens its own ``ffmpeg_source`` and composites it on a PRIVATE
``obs_view``, exactly like the virtual camera composites its own mix. Nothing
here touches channel 0 or the programme.

Three things about private views cost real debugging time and are why this module
looks the way it does:

* A private view does **not activate** a media source. An ``ffmpeg_source`` put
  on a view channel sits at ``media_state`` NONE with 0x0 dimensions and the mix
  stays black; it only decodes once libobs counts it active *and* showing. A
  *colour* source renders regardless, which makes a broken harness look healthy —
  always test this with real media.
* Global channels would activate it, but they feed the programme. Not an option.
* A source dropped straight on a channel renders at its native size anchored
  top-left, so it goes in a scene carrying letterbox bounds instead.

Audio is kept out of every output mix (``audio_mixers = 0``) and reaches the
operator only through libobs *monitoring*. That is what makes unmuting safe: the
programme mix and the virtual camera cannot pick it up, because it was never in
a mix to begin with.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from .playback_state import MediaStatus, PlaybackState

log = logging.getLogger(__name__)

#: libobs ``enum video_format`` / range / colorspace for the frame tap.
_VIDEO_FORMAT_BGRA = 7
_VIDEO_RANGE_FULL = 1
_VIDEO_CS_709 = 2

#: obs_media_state values (see obs_playback for the same table).
_OBS_STATE_NONE = 0
_OBS_STATE_PLAYING = 1
_OBS_STATE_OPENING = 2
_OBS_STATE_BUFFERING = 3
_OBS_STATE_PAUSED = 4
_OBS_STATE_STOPPED = 5
_OBS_STATE_ENDED = 6
_OBS_STATE_ERROR = 7

#: The view renders only while an output consumes its mix — a raw callback alone
#: yields zero frames. Raw video into the null muxer costs no encoding.
_PUMP_KIND = "ffmpeg_output"

_CH_PREVIEW = 0

#: A trim preview is a small dialog surface; full frame rate would burn CPU on
#: conversions nobody can see the benefit of.
_FRAME_INTERVAL_MS = 66  # ~15 fps
_POLL_INTERVAL_MS = 100

#: Preview surface size. Scaling happens inside libobs, so the tap only ever
#: copies this much per frame regardless of the source resolution.
PREVIEW_WIDTH = 854
PREVIEW_HEIGHT = 480


def _pump_settings() -> dict:
    import sys

    return {
        "url": "NUL" if sys.platform == "win32" else "/dev/null",
        "format_name": "null",
        "video_encoder": "rawvideo",
        "audio_encoder": "pcm_s16le",
    }


class ObsPreviewPlayer(QObject):
    """A single media file playing on a private libobs mix.

    Exposes the subset of ``QMediaPlayer`` semantics the trim dialog needs.
    Values use Qt's numbering (see :mod:`playback_state`) so the QML comparisons
    that used to run against ``MediaPlayer.*`` keep their meaning.
    """

    duration_changed = Signal(int)          # ms
    position_changed = Signal(int)          # ms
    playback_state_changed = Signal(int)    # PlaybackState
    media_status_changed = Signal(int)      # MediaStatus
    seekable_changed = Signal(bool)
    error_occurred = Signal(str)
    frame_ready = Signal(QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = None
        self._scene = None
        self._view = None
        self._video = None
        self._output = None
        self._activated = False

        self._callback = None    # cffi trampoline; libobs holds a raw pointer
        self._conversion = None  # cffi struct; must outlive the connect call
        self._ffi = None
        self._lib = None

        self._frame_lock = threading.Lock()
        self._latest: tuple[bytes, int, int, int] | None = None

        self._url = ""
        self._duration = 0
        self._position = 0
        self._state = PlaybackState.StoppedState
        self._status = MediaStatus.NoMedia
        self._seekable = False
        self._muted = True
        self._volume = 0.72
        #: Set while a seek is in flight so position reads do not report a stale
        #: poll value — the dialog reads position immediately after seeking.
        self._pending_seek: int | None = None
        #: False until play() is called, so the source's own autoplay can be
        #: suppressed without fighting a deliberate play.
        self._started_by_user = False

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._on_poll)

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(_FRAME_INTERVAL_MS)
        self._frame_timer.timeout.connect(self._emit_frame)

    # ── State accessors ───────────────────────────────────────────────────

    @property
    def duration(self) -> int:
        return self._duration

    @property
    def position(self) -> int:
        # A seek is applied to libobs immediately but only shows up in
        # media_time on a later tick; report the requested position so a read
        # right after a seek is not a stale poll value.
        if self._pending_seek is not None:
            return self._pending_seek
        return self._position

    @property
    def playback_state(self) -> int:
        return int(self._state)

    @property
    def media_status(self) -> int:
        return int(self._status)

    @property
    def seekable(self) -> bool:
        return self._seekable

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def volume(self) -> float:
        return self._volume

    # ── Public API ────────────────────────────────────────────────────────

    def set_source(self, url: str) -> None:
        """Load ``url``. An empty string unloads without reporting an error."""
        self.stop()
        self._url = url or ""
        if not self._url:
            self._set_status(MediaStatus.NoMedia)
            return
        self._set_status(MediaStatus.LoadingMedia)
        if not self._ensure_harness():
            self._fail("Solin's media engine is unavailable.")
            return
        if not self._open_source():
            self._fail("The media could not be opened.")
            return
        # ffmpeg_source starts decoding the moment it becomes active, but
        # QMediaPlayer semantics are "assigning source loads, it does not play"
        # — and the dialog relies on that: togglePreview() checks playbackState
        # first, so an auto-playing source would make the first click PAUSE.
        try:
            self._source.media_play_pause(True)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not pause the freshly loaded preview", exc_info=True)
        self._started_by_user = False
        self._set_state(PlaybackState.StoppedState)
        self._poll.start()
        # Keep the surface fed while a clip is loaded, not just while playing:
        # otherwise the preview stays blank until the operator presses play, and
        # scrubbing a paused clip would not repaint.
        self._frame_timer.start()

    def play(self) -> None:
        if self._source is None:
            return
        self._started_by_user = True
        try:
            self._source.media_play_pause(False)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview play failed", exc_info=True)
            return
        self._frame_timer.start()
        self._set_state(PlaybackState.PlayingState)

    def pause(self) -> None:
        if self._source is None:
            return
        try:
            self._source.media_play_pause(True)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview pause failed", exc_info=True)
            return
        self._set_state(PlaybackState.PausedState)

    def stop(self) -> None:
        self._started_by_user = False
        self._poll.stop()
        self._frame_timer.stop()
        self._teardown_source()
        self._duration = 0
        self._position = 0
        self._pending_seek = None
        self._set_seekable(False)
        self._set_state(PlaybackState.StoppedState)

    def seek(self, ms: int) -> None:
        if self._source is None:
            return
        target = max(0, int(ms))
        if self._duration > 0:
            target = min(target, self._duration)
        self._pending_seek = target
        try:
            if self._status == MediaStatus.EndOfMedia:
                # A finished ffmpeg_source ignores a seek — the decoder is done.
                # Restart it (paused) so the operator can still scrub back into
                # the clip after it has played through.
                self._source.media_restart()
                self._source.media_play_pause(True)
                self._set_status(MediaStatus.LoadedMedia)
            self._source.media_time = target
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview seek failed", exc_info=True)
            self._pending_seek = None
            return
        self._position = target
        self.position_changed.emit(target)
        # Paused seeks still need to repaint the surface at the new position.
        self._frame_timer.start()

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._apply_audio()

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        self._apply_audio()

    def shutdown(self) -> None:
        self.stop()
        self._teardown_harness()

    # ── Audio ─────────────────────────────────────────────────────────────

    def _apply_audio(self) -> None:
        """Route audio to the operator only — never into an output mix.

        ``audio_mixers = 0`` keeps the source out of every mix (programme,
        recording, virtual camera), so the only way it can be heard is libobs
        monitoring, which is a local playback path. That is what makes unmuting
        the preview safe during a live meeting.
        """
        source = self._source
        if source is None:
            return
        try:
            source.audio_mixers = 0
            source.volume = 0.0 if self._muted else float(self._volume)
            source.muted = bool(self._muted)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not apply preview audio state", exc_info=True)
        try:
            from .obs_runtime import MONITORING_MONITOR_ONLY, obs_runtime

            monitoring = 0 if self._muted else MONITORING_MONITOR_ONLY
            obs_runtime().set_source_monitoring(source, monitoring)
        except Exception:  # noqa: BLE001 - monitoring is best-effort
            log.debug("Could not set preview monitoring", exc_info=True)

    # ── Source lifecycle ──────────────────────────────────────────────────

    def _open_source(self) -> bool:
        from .obs_runtime import obs_runtime

        runtime = obs_runtime()
        ob = runtime.ob
        is_remote = self._url.startswith(("http://", "https://"))
        local = self._local_path(self._url)
        if is_remote:
            settings = {"is_local_file": False, "input": self._url}
        else:
            settings = {"is_local_file": True, "local_file": local}
        # Deliberately NO hw_decode: hardware decode availability varies per
        # machine and a failed DXVA2 init sends ffmpeg_source straight to ENDED
        # with 0x0 dimensions — a silently black preview.
        try:
            source = ob.Source.create_private("ffmpeg_source", "solin-trim-preview", settings)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview source creation failed for %r", self._url, exc_info=True)
            return False
        if source is None:
            return False
        self._source = source
        self._apply_audio()

        try:
            canvas = runtime.video
            scene = ob.Scene.create("solin-trim-preview-scene")
            item = scene.add(source)
            item.bounds_type = int(ob.BoundsType.SCALE_INNER)
            item.bounds = (float(canvas.width), float(canvas.height))
            item.bounds_alignment = int(ob.Alignment.CENTER)
            self._scene = scene
            self._view.set_source(_CH_PREVIEW, scene.as_source())
            from pylibobs._ffi import get_lib

            lib = get_lib()
            lib.obs_source_inc_active(source._ptr)
            lib.obs_source_inc_showing(source._ptr)
            self._activated = True
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not compose the preview source", exc_info=True)
            self._teardown_source()
            return False
        return True

    @staticmethod
    def _local_path(url: str) -> str:
        """QML hands over ``file:///C:/...`` URLs; ffmpeg_source wants a path."""
        if not url.startswith("file:"):
            return url
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        path = unquote(parsed.path)
        # "/C:/x" → "C:/x" on Windows; POSIX paths are already correct.
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path

    def _teardown_source(self) -> None:
        source, self._source = self._source, None
        if self._activated and source is not None:
            self._activated = False
            try:
                from pylibobs._ffi import get_lib

                lib = get_lib()
                lib.obs_source_dec_showing(source._ptr)
                lib.obs_source_dec_active(source._ptr)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not drop preview refcounts", exc_info=True)
        if self._view is not None:
            try:
                self._view.set_source(_CH_PREVIEW, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not unbind the preview source", exc_info=True)
        scene, self._scene = self._scene, None
        for obj in (scene, source):
            if obj is None:
                continue
            try:
                obj.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release a preview object", exc_info=True)
        with self._frame_lock:
            self._latest = None

    # ── Polling ───────────────────────────────────────────────────────────

    def _on_poll(self) -> None:
        source = self._source
        if source is None:
            return
        try:
            obs_state = int(source.media_state)
            duration = int(source.media_duration)
            position = max(0, int(source.media_time))
        except Exception:  # noqa: BLE001 - libobs boundary
            return

        if duration > 0 and duration != self._duration:
            self._duration = duration
            self.duration_changed.emit(duration)
            # ffmpeg_source seeks any container it could open; reporting
            # seekable alongside a known duration is what unblocks the dialog.
            self._set_seekable(True)
            self._set_status(MediaStatus.LoadedMedia)

        if self._pending_seek is not None and abs(position - self._pending_seek) < 400:
            self._pending_seek = None
        if self._pending_seek is None and position != self._position:
            self._position = position
            self.position_changed.emit(position)

        if obs_state == _OBS_STATE_PLAYING:
            if not self._started_by_user:
                # ffmpeg_source begins playing as soon as it is activated, and a
                # pause issued right after creation lands before the source has
                # finished opening, so it does not stick. Catching it here — the
                # first tick where it genuinely reports PLAYING — is what makes
                # "assigning source loads but does not play" hold. Without it the
                # operator's first click on Play would PAUSE instead.
                try:
                    source.media_play_pause(True)
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("Could not suppress preview autoplay", exc_info=True)
                self._set_state(PlaybackState.StoppedState)
                return
            self._set_state(PlaybackState.PlayingState)
        elif obs_state == _OBS_STATE_PAUSED:
            self._set_state(PlaybackState.PausedState)
        elif obs_state in (_OBS_STATE_STOPPED, _OBS_STATE_ENDED):
            self._set_state(PlaybackState.StoppedState)
            if obs_state == _OBS_STATE_ENDED:
                self._set_status(MediaStatus.EndOfMedia)
        elif obs_state == _OBS_STATE_ERROR:
            self._fail("Playback failed.")
        elif obs_state in (_OBS_STATE_OPENING, _OBS_STATE_BUFFERING):
            self._set_status(MediaStatus.BufferingMedia)

    # ── Change plumbing ───────────────────────────────────────────────────

    def _set_state(self, state: PlaybackState) -> None:
        if self._state == state:
            return
        self._state = state
        self.playback_state_changed.emit(int(state))

    def _set_status(self, status: MediaStatus) -> None:
        if self._status == status:
            return
        self._status = status
        self.media_status_changed.emit(int(status))

    def _set_seekable(self, seekable: bool) -> None:
        if self._seekable == seekable:
            return
        self._seekable = seekable
        self.seekable_changed.emit(seekable)

    def _fail(self, message: str) -> None:
        self._set_status(MediaStatus.InvalidMedia)
        self._set_state(PlaybackState.StoppedState)
        self._frame_timer.stop()
        self.error_occurred.emit(message)

    # ── Frame tap ─────────────────────────────────────────────────────────

    def _emit_frame(self) -> None:
        with self._frame_lock:
            latest, self._latest = self._latest, None
        if latest is None:
            return
        if self._status == MediaStatus.EndOfMedia:
            # Once the clip ends the mix goes black. Holding the last decoded
            # frame matches QMediaPlayer and keeps the operator looking at the
            # picture they were trimming instead of a black rectangle.
            return
        data, width, height, stride = latest
        image = QImage(data, width, height, stride, QImage.Format.Format_ARGB32).copy()
        if not image.isNull():
            self.frame_ready.emit(image)

    # ── libobs harness ────────────────────────────────────────────────────

    def _ensure_harness(self) -> bool:
        if self._view is not None and self._callback is not None:
            return True
        try:
            from .obs_runtime import obs_runtime

            runtime = obs_runtime()
            runtime.ensure_started()
            ob = runtime.ob
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview harness: runtime unavailable", exc_info=True)
            return False
        try:
            if self._view is None:
                self._view = ob.View.create()
            if self._video is None:
                self._video = self._view.add()
            if self._video is None or not self._connect_tap():
                self._teardown_harness()
                return False
            if self._output is None:
                if _PUMP_KIND not in set(ob.enum_output_types()):
                    log.debug("Preview harness: no %r output", _PUMP_KIND)
                    self._teardown_harness()
                    return False
                self._output = ob.Output.create(_PUMP_KIND, "solin-trim-pump", _pump_settings())
                # The pump declares an audio encoder, so it needs a real audio_t:
                # given None it waits forever for audio and pulls no video.
                audio = None
                try:
                    audio = runtime.context.get_audio()
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("Could not get the obs audio handle", exc_info=True)
                self._output.set_media(self._video, audio)
                if not self._output.start():
                    log.debug("Preview harness: pump refused to start")
                    self._teardown_harness()
                    return False
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Preview harness construction failed", exc_info=True)
            self._teardown_harness()
            return False
        return True

    def _connect_tap(self) -> bool:
        if self._callback is not None:
            return True
        try:
            from pylibobs._ffi import ffi, get_lib
        except Exception:  # noqa: BLE001 - optional dependency
            log.debug("Preview harness: pylibobs ffi unavailable", exc_info=True)
            return False
        lib = get_lib()
        frame_lock = self._frame_lock

        @ffi.callback("void(void*, struct video_data*)")
        def _on_frame(_param, frame):  # libobs graphics thread — keep it short
            try:
                stride = int(frame.linesize[0])
                if stride <= 0:
                    return
                buf = bytes(ffi.buffer(frame.data[0], stride * PREVIEW_HEIGHT))
                with frame_lock:
                    self._latest = (buf, PREVIEW_WIDTH, PREVIEW_HEIGHT, stride)
            except Exception:  # noqa: BLE001 - never raise into libobs
                log.debug("Preview frame tap failed", exc_info=True)

        conversion = ffi.new("struct video_scale_info *")
        conversion.format = _VIDEO_FORMAT_BGRA
        conversion.width = PREVIEW_WIDTH
        conversion.height = PREVIEW_HEIGHT
        conversion.range = _VIDEO_RANGE_FULL
        conversion.colorspace = _VIDEO_CS_709
        self._conversion = conversion  # libobs keeps the pointer

        if not lib.video_output_connect(self._video, conversion, _on_frame, ffi.NULL):
            log.debug("Preview harness: video_output_connect refused the mix")
            self._conversion = None
            return False
        self._callback = _on_frame
        self._ffi = ffi
        self._lib = lib
        return True

    def _teardown_harness(self) -> None:
        if self._callback is not None and self._lib is not None:
            try:
                self._lib.video_output_disconnect(self._video, self._callback, self._ffi.NULL)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not disconnect the preview tap", exc_info=True)
        self._callback = None
        self._conversion = None
        self._lib = None
        self._ffi = None
        if self._output is not None:
            try:
                self._output.stop()
                self._output.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release the preview pump", exc_info=True)
            self._output = None
        if self._view is not None:
            try:
                self._view.set_source(_CH_PREVIEW, None)
                self._view.remove()
                self._view.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release the preview view", exc_info=True)
            self._view = None
        self._video = None


__all__ = ["ObsPreviewPlayer", "PREVIEW_WIDTH", "PREVIEW_HEIGHT"]
