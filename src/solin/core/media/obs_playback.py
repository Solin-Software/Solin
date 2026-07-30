"""libobs-backed playback engine exposing the :class:`MediaController` surface.

This is the pylibobs counterpart to :class:`solin.core.media.playback.MediaController`.
It keeps the *same* public Qt signals, methods and the ``audio_output`` shim so
every existing consumer (projection bar, remote control, background-song service,
notifications) works unchanged — only the decode/output engine differs:

    QMediaPlayer + QAudioOutput   →   libobs ffmpeg_source
                                      (+ audio monitoring to speakers)

Video is **not** delivered as ``frame_ready`` frames here: under the libobs
architecture the projection/preview surfaces host their own ``Display`` that
renders the shared OBS canvas (see the projection layer).  ``frame_ready`` is kept
in the signal list for interface parity but is not emitted.

Download/cache behaviour is preserved: remote sources are resolved through the
same :class:`MediaCacheManager`/downloader and libobs plays a resolved local file
whenever one is available, falling back to streaming the URL directly (ffmpeg_source
buffers internally) while the download populates the cache.

Scope notes (documented, not silently dropped):
* Cover-art / title metadata: libobs' ffmpeg_source exposes no tags, so these are
  read from the file directly with mutagen (pure-Python, no Qt) — see _MetadataProbe.
* Live-stream custom reconnect is delegated to ffmpeg_source's own buffering.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame

from .cache import MediaCacheManager
from .obs_runtime import MONITORING_MONITOR_ONLY, ObsRuntimeError, obs_runtime
from .playback_request import MediaPlaybackRequest, ResolvedPlaybackRange
from .qt_contracts import PlaybackDownloaderFactory
from .settings import MediaPlaybackSettings

log = logging.getLogger(__name__)

# obs_media_state enum values (obs/media-io/media-remux etc.).
_OBS_STATE_NONE = 0
_OBS_STATE_PLAYING = 1
_OBS_STATE_OPENING = 2
_OBS_STATE_BUFFERING = 3
_OBS_STATE_PAUSED = 4
_OBS_STATE_STOPPED = 5
_OBS_STATE_ENDED = 6
_OBS_STATE_ERROR = 7

_POLL_INTERVAL_MS = 100
_FRAME_INTERVAL_MS = 33  # ~30 fps preview-frame emission when enabled


class _ObsAudioOutput:
    """Minimal ``QAudioOutput``-compatible shim backed by a libobs source.

    :class:`~solin.core.jw.background_song_service.BackgroundSongService` drives
    volume/fade by reaching into ``controller.audio_output`` directly, so the
    libobs engine must expose the same ``setVolume``/``volume``/``setMuted``/
    ``isMuted`` methods.  Values are remembered so they survive source swaps.
    """

    def __init__(self) -> None:
        self._volume = 1.0
        self._muted = False
        self._source = None  # current pylibobs Source, if any

    def bind_source(self, source) -> None:
        self._source = source
        if source is not None:
            self._apply()

    def reapply(self) -> None:
        """Re-apply the remembered volume/mute to the bound source — e.g. after a
        temporary trim pre-roll mute is lifted (see _resolve_trim)."""
        self._apply()

    def _apply(self) -> None:
        source = self._source
        if source is None:
            return
        try:
            source.volume = 0.0 if self._muted else float(self._volume)
            source.muted = bool(self._muted)
        except Exception:  # noqa: BLE001 - libobs source boundary
            log.debug("Could not apply audio state to libobs source", exc_info=True)

    def setVolume(self, value: float) -> None:  # noqa: N802 - Qt-compatible name
        self._volume = max(0.0, min(1.0, float(value)))
        self._apply()

    def volume(self) -> float:
        return self._volume

    def setMuted(self, muted: bool) -> None:  # noqa: N802 - Qt-compatible name
        self._muted = bool(muted)
        self._apply()

    def isMuted(self) -> bool:  # noqa: N802 - Qt-compatible name
        return self._muted


class _ObsPlayerShim:
    """Minimal ``QMediaPlayer``-compatible surface for callers that reach into
    ``media_controller.player`` directly (playlist live-thumb capture, the
    projection bar's playback-state overlay, the remote-control controller).

    The libobs engine has no ``QMediaPlayer``; this exposes the two members
    those call sites use — ``playbackState()`` and ``metaData()`` — backed by the
    controller's tracked state and its metadata probe.
    """

    def __init__(self, controller: "ObsMediaController") -> None:
        self._controller = controller

    def playbackState(self):  # noqa: N802 - Qt-compatible name
        return self._controller._last_state

    def metaData(self):  # noqa: N802 - Qt-compatible name
        from PySide6.QtMultimedia import QMediaMetaData

        md = QMediaMetaData()
        probe = getattr(self._controller, "_metadata_probe", None)
        title = getattr(probe, "_last_title", "") if probe is not None else ""
        if title:
            md.insert(QMediaMetaData.Key.Title, title)
        return md


def _read_media_tags(path: str) -> tuple[str | None, object]:
    """Read a local media file's title + embedded cover art with mutagen.

    Pure-Python and cross-platform (no Qt, no ffprobe binary). Returns
    ``(title, cover)`` where ``cover`` is a ``QPixmap`` or ``None``. Any failure
    (missing mutagen, unsupported/corrupt file, no tags) degrades to ``None``.
    """
    try:
        import mutagen
    except Exception:  # noqa: BLE001 - optional dependency / import boundary
        return None, None

    title: str | None = None
    try:
        easy = mutagen.File(path, easy=True)
        if easy is not None:
            values = easy.get("title") or easy.get("\xa9nam")
            if values:
                text = str(values[0]).strip()
                title = text or None
    except Exception:  # noqa: BLE001 - tag-read boundary
        title = None

    cover: object = None
    try:
        raw = mutagen.File(path)
        data = _extract_cover_bytes(raw)
        if data:
            image = QImage.fromData(data)
            if not image.isNull():
                cover = QPixmap.fromImage(image)
    except Exception:  # noqa: BLE001 - tag-read boundary
        cover = None
    return title, cover


def _extract_cover_bytes(media) -> bytes | None:
    """Pull the first embedded cover image's bytes from a mutagen file object,
    across the tag formats Solin's media use (FLAC/OGG pictures, ID3 APIC, MP4
    ``covr``)."""
    if media is None:
        return None
    pictures = getattr(media, "pictures", None)  # FLAC / OGG
    if pictures:
        return bytes(pictures[0].data)
    tags = getattr(media, "tags", None)
    if tags is None:
        return None
    getall = getattr(tags, "getall", None)
    if callable(getall):  # ID3 APIC frames
        try:
            apics = getall("APIC")
        except Exception:  # noqa: BLE001 - tag boundary
            apics = []
        if apics:
            return bytes(apics[0].data)
    try:
        covr = tags.get("covr")  # MP4 cover atom
    except Exception:  # noqa: BLE001 - tag boundary
        covr = None
    if covr:
        return bytes(covr[0])
    return None


class _MetadataProbe(QObject):
    """Recovers a media file's title + cover art for the operator UI — with
    mutagen, not Qt.

    libobs' ``ffmpeg_source`` exposes no media tags, and the obs engine keeps no
    ``QMediaPlayer``, so this reads the file's tags directly. LOCAL files only
    (mutagen can't open a remote URL); remote media is re-probed once cached, and
    until then the cover is cleared so no stale art lingers.
    """

    title_found = Signal(str)
    cover_found = Signal(object)  # QPixmap | None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._last_title = ""  # exposed via the .player metaData() shim

    def probe(self, source: str) -> None:
        if not source or MediaCacheManager.is_remote(source):
            self._last_title = ""
            self.cover_found.emit(None)  # clear stale art; re-probed once cached
            return
        title, cover = _read_media_tags(source)
        self._last_title = title or ""
        if title:
            self.title_found.emit(title)
        self.cover_found.emit(cover)

    def stop(self) -> None:
        self._last_title = ""


class ObsMediaController(QObject):
    """Drop-in libobs playback engine mirroring :class:`MediaController`."""

    frame_ready = Signal(object)  # kept for parity; not emitted (Display renders)
    state_changed = Signal(QMediaPlayer.PlaybackState)
    duration_changed = Signal(int)
    source_duration_changed = Signal(int)
    position_changed = Signal(int)
    error_occurred = Signal(str)
    playback_interrupted = Signal(str, str)
    playback_recovery_changed = Signal(bool)
    media_ended = Signal()
    cover_art_changed = Signal(object)
    title_from_metadata = Signal(str)
    playback_download_failed = Signal(str, str, bool)
    playback_range_changed = Signal(int, int)
    buffer_progress = Signal(int, int)
    playback_source_changed = Signal(bool)

    def __init__(
        self,
        settings: MediaPlaybackSettings,
        cache_manager: MediaCacheManager,
        *,
        downloader_factory: PlaybackDownloaderFactory,
        projection: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache_manager = cache_manager
        self._downloader = downloader_factory(self)

        self.audio_output = _ObsAudioOutput()
        # QMediaPlayer-compatible shim for code that reaches into ``.player``.
        self.player = _ObsPlayerShim(self)

        self._runtime = obs_runtime()
        # The foreground engine hands its source to the projection program
        # (channel-0 fade transition) so switching media crossfades. Audio-only
        # engines (background songs) keep a private channel — no visible output.
        self._projection = projection
        self._channel: int | None = None
        self._source = None  # pylibobs Source (the ffmpeg_source; media control target)
        self._session_id = 0
        self._current_url = ""
        self._local_path: str | None = None
        self._stream_persist = False

        self._request: MediaPlaybackRequest | None = None
        self._playback_range: ResolvedPlaybackRange | None = None
        # A trimmed clip decodes from position 0 until the first poll seeks it to
        # start_ms; muted until then so no wrong-position audio blips out.
        self._trim_muted = False
        self._requested_playing = False
        self._duration_ms = 0
        self._duration_emitted = False
        self._last_state = QMediaPlayer.PlaybackState.StoppedState
        self._ended_emitted = False

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._on_poll)

        # Gated preview-frame output: the libobs raw callback buffers the latest
        # composited frame (on the graphics thread); a timer emits it as a
        # QVideoFrame via frame_ready, but only while an operator surface (preview
        # or fullscreen) has requested output. Projection uses the Display, so it
        # needs no frames — this exists purely for the Qt-painted operator views.
        self._frame_lock = threading.Lock()
        self._latest_frame: tuple[bytes, int, int, int] | None = None
        self._raw_cb = None
        self._frame_output_requested = False
        self._frame_output_active = False
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(_FRAME_INTERVAL_MS)
        self._frame_timer.timeout.connect(self._emit_buffered_frame)

        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.finished.connect(self._on_download_finished)
        self._downloader.error.connect(self._on_download_error)

        # libobs exposes no media tags, so recover title/cover via a Qt probe.
        self._metadata_probe = _MetadataProbe(self)
        self._metadata_probe.title_found.connect(self.title_from_metadata)
        self._metadata_probe.cover_found.connect(self.cover_art_changed)

    # ── Read-only properties (interface parity) ───────────────────────────

    @property
    def current_url(self) -> str:
        return self._current_url

    @property
    def local_path(self) -> str | None:
        return self._local_path

    @property
    def stream_persist(self) -> bool:
        return self._stream_persist

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def volume(self) -> float:
        return self.audio_output.volume()

    @property
    def is_recovering(self) -> bool:
        return False

    @property
    def is_playing(self) -> bool:
        return self._last_state == QMediaPlayer.PlaybackState.PlayingState

    @property
    def is_paused(self) -> bool:
        return self._last_state == QMediaPlayer.PlaybackState.PausedState

    @property
    def duration(self) -> int:
        if self._playback_range is not None:
            return self._playback_range.duration_ms
        return self._duration_ms

    @property
    def position(self) -> int:
        pos = self._media_time()
        if self._playback_range is not None:
            return max(0, min(pos - self._playback_range.start_ms,
                              self._playback_range.duration_ms))
        return pos

    # ── Public playback API ───────────────────────────────────────────────

    def start_playback(self, request: MediaPlaybackRequest) -> None:
        if not isinstance(request, MediaPlaybackRequest):
            raise TypeError("request must be a MediaPlaybackRequest")
        try:
            self._runtime.ensure_started(
                width=self._runtime.video.width,
                height=self._runtime.video.height,
                fps=self._runtime.video.fps,
            )
        except ObsRuntimeError as exc:
            self.error_occurred.emit(str(exc))
            return

        self._teardown_source()
        url = request.source
        self._request = request
        self._playback_range = None
        self._requested_playing = request.autoplay
        self._current_url = url
        self._session_id += 1
        self._duration_ms = 0
        self._duration_emitted = False
        self._ended_emitted = False
        self.buffer_progress.emit(0, 0)

        is_remote = MediaCacheManager.is_remote(url)
        if not is_remote:
            self._local_path = url
            self._stream_persist = False
            self._play_local(url)
            self._metadata_probe.probe(url)
            self.playback_source_changed.emit(True)
            return

        cached = self._downloader.get_cached_path(url)
        if cached:
            self._downloader.cancel()
            self._local_path = cached
            self._stream_persist = True
            import os

            size = os.path.getsize(cached)
            self.buffer_progress.emit(size, size)
            self._play_local(cached)
            self._metadata_probe.probe(cached)
            self.playback_source_changed.emit(True)
            return

        # Remote, not cached: stream the URL directly through ffmpeg_source while
        # the downloader populates the cache (buffer bar + later switch-to-local).
        self._local_path = None
        from .playback_request import PlaybackCachePolicy

        if request.cache_policy is PlaybackCachePolicy.PROFILE_DEFAULT:
            self._stream_persist = self._settings.auto_download_on_play()
        else:
            self._stream_persist = request.cache_policy is PlaybackCachePolicy.PERSISTENT
        self._play_stream(url)
        self._metadata_probe.probe(url)
        self._downloader.start(url, persist=self._stream_persist)
        self.playback_source_changed.emit(False)

    def play(self) -> None:
        self._requested_playing = True
        if self._source is not None:
            self._source.media_play_pause(False)

    def pause(self) -> None:
        self._requested_playing = False
        if self._source is not None:
            self._source.media_play_pause(True)

    def stop(self) -> None:
        self._poll.stop()
        self._downloader.cancel()
        self._metadata_probe.stop()
        self._teardown_source()
        self._request = None
        self._playback_range = None
        self._requested_playing = False
        self._current_url = ""
        self._local_path = None
        self._duration_ms = 0
        self._duration_emitted = False
        self._ended_emitted = False
        self.buffer_progress.emit(0, 0)
        self.playback_source_changed.emit(False)
        self._emit_state(QMediaPlayer.PlaybackState.StoppedState)

    def toggle_play_pause(self) -> None:
        if self._last_state == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def replay(self) -> None:
        if self._source is None:
            return
        self._ended_emitted = False
        self._requested_playing = True
        start_ms = self._playback_range.start_ms if self._playback_range else 0
        self._source.media_restart()
        if start_ms:
            self._source.media_time = start_ms

    def seek(self, ms: int) -> None:
        if self._source is None:
            return
        relative = max(0, int(ms))
        if self._playback_range is None:
            self._source.media_time = relative
            return
        absolute = self._playback_range.start_ms + min(
            relative, self._playback_range.duration_ms
        )
        self._source.media_time = absolute

    def set_volume(self, value: float) -> None:
        self.audio_output.setVolume(value)

    def set_playback_rate(self, rate: float) -> None:
        if self._source is None:
            return
        # ffmpeg_source expects an integer "speed_percent"; libobs resets any
        # value outside 1..200 back to 100, so clamp here.
        speed_percent = max(1, min(200, round(rate * 100)))
        try:
            self._source.update(self._runtime.ob.OBSData({"speed_percent": speed_percent}))
        except Exception:  # noqa: BLE001 - optional feature, libobs boundary
            log.debug("Could not set libobs playback speed", exc_info=True)

    def set_frame_output_enabled(self, enabled: bool) -> None:
        """Request/stop emission of preview frames via ``frame_ready``.

        The operator preview and fullscreen overlay are Qt-painted and consume
        ``frame_ready``; the projection windows render libobs directly and do
        not. To avoid paying for a full-frame copy every tick when nobody is
        watching, the raw libobs callback is only attached while an operator
        surface asks for it.
        """
        self._frame_output_requested = bool(enabled)
        self._refresh_frame_output()

    # ── Preview-frame output (raw callback → frame_ready) ──────────────────

    def _refresh_frame_output(self) -> None:
        should_run = (
            self._frame_output_requested
            and self._source is not None
            and self._runtime.started
        )
        if should_run and not self._frame_output_active:
            self._start_frame_output()
        elif not should_run and self._frame_output_active:
            self._stop_frame_output()

    def _start_frame_output(self) -> None:
        ob = self._runtime.ob
        canvas = self._runtime.video
        try:
            self._raw_cb = ob.add_raw_video_callback(
                self._on_raw_frame,
                format=int(ob.VideoFormat.BGRA),
                width=canvas.width,
                height=canvas.height,
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not register libobs raw video callback", exc_info=True)
            return
        self._frame_output_active = True
        self._frame_timer.start()

    def _stop_frame_output(self) -> None:
        self._frame_timer.stop()
        if self._raw_cb is not None:
            try:
                self._runtime.ob.remove_raw_video_callback(self._raw_cb)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not remove libobs raw video callback", exc_info=True)
            self._raw_cb = None
        with self._frame_lock:
            self._latest_frame = None
        self._frame_output_active = False

    def _on_raw_frame(self, planes, linesizes, width, height, _fmt, _ts) -> None:
        # libobs graphics thread: buffer the latest frame cheaply; the GUI-thread
        # timer converts and emits it (coalescing dropped frames).
        data = planes[0] if planes else b""
        if not data or width <= 0 or height <= 0:
            return
        with self._frame_lock:
            self._latest_frame = (data, int(width), int(height), int(linesizes[0]))

    def _emit_buffered_frame(self) -> None:
        with self._frame_lock:
            latest = self._latest_frame
            self._latest_frame = None
        if latest is None:
            return
        data, width, height, stride = latest
        image = QImage(data, width, height, stride, QImage.Format.Format_ARGB32).copy()
        if image.isNull():
            return
        # QVideoFrame(QImage) is valid at runtime; the bundled stubs omit the overload.
        frame = QVideoFrame(image)  # pyright: ignore[reportCallIssue, reportArgumentType]
        self.frame_ready.emit(frame)

    # ── Source lifecycle ──────────────────────────────────────────────────

    def _play_local(self, path: str) -> None:
        self._create_source({"is_local_file": True, "local_file": path})

    def _play_stream(self, url: str) -> None:
        self._create_source({"is_local_file": False, "input": url})

    def _create_source(self, settings: dict) -> None:
        ob = self._runtime.ob
        try:
            source = ob.Source.create("ffmpeg_source", f"solin-media-{self._session_id}", settings)
        except Exception as exc:  # noqa: BLE001 - source creation boundary
            self.error_occurred.emit(f"Could not open media: {exc}")
            return
        self._source = source
        self.audio_output.bind_source(source)
        # Trim pre-roll gate: a custom-trimmed clip starts decoding at position 0
        # and only seeks to start_ms on the first poll (once duration is known),
        # so mute it now — otherwise a fraction of a second of wrong-position audio
        # (and video) blips out before the seek lands. Lifted in _resolve_trim.
        request = self._request
        self._trim_muted = bool(
            request is not None and request.trim is not None and request.trim.custom
        )
        if self._trim_muted:
            try:
                source.muted = True
                source.volume = 0.0
            except Exception:  # noqa: BLE001 - libobs source boundary
                log.debug("Could not mute source for trim pre-roll", exc_info=True)
                self._trim_muted = False
        if self._projection:
            # Hand the source to the projection program, which wraps it in a
            # canvas-filling scene and crossfades from whatever was showing.
            from .obs_program import projection_program

            program = projection_program()
            program.ensure()
            program.show_media(source)
        else:
            # Audio-only: keep the source active on a private channel (no scene,
            # no visible output) so its audio is decoded and monitored.
            if self._channel is None:
                self._channel = self._runtime.acquire_channel()
            self._runtime.set_channel_source(self._channel, source)
        self._runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
        source.media_play_pause(not self._requested_playing)
        self._poll.start()
        self._emit_state(
            QMediaPlayer.PlaybackState.PlayingState
            if self._requested_playing
            else QMediaPlayer.PlaybackState.PausedState
        )
        self._refresh_frame_output()

    def _teardown_source(self) -> None:
        if self._source is None:
            return
        self.audio_output.bind_source(None)
        if self._projection:
            # The projection program owns the source's disposal: it keeps the
            # source playing through the crossfade-out, then stops + releases it.
            # Releasing here would run obs_source_remove and yank the source out
            # of the live media scene mid-fade — the video would cut to black
            # instead of dissolving. Hand it off; do NOT stop or release it.
            try:
                from .obs_program import projection_program

                projection_program().detach_media()
            except Exception:  # noqa: BLE001 - libobs/program boundary
                log.debug("Could not hand media source to program for fade-out", exc_info=True)
            self._source = None
            self._refresh_frame_output()
            return
        # Audio-only engine (background songs): stop + release immediately; there
        # is no visible scene to fade, only a private audio channel to clear.
        try:
            self._source.media_stop()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("libobs media_stop errored during teardown", exc_info=True)
        if self._channel is not None:
            try:
                self._runtime.set_channel_source(self._channel, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not clear libobs channel", exc_info=True)
        try:
            self._source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("libobs source release errored", exc_info=True)
        self._source = None
        self._refresh_frame_output()

    def shutdown(self) -> None:
        self._poll.stop()
        self._stop_frame_output()
        self._teardown_source()
        if self._channel is not None:
            self._runtime.release_channel(self._channel)
            self._channel = None

    # ── Polling → signals ─────────────────────────────────────────────────

    def _media_time(self) -> int:
        if self._source is None:
            return 0
        try:
            return max(0, int(self._source.media_time))
        except Exception:  # noqa: BLE001 - libobs boundary
            return 0

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

        if duration > 0 and not self._duration_emitted:
            self._duration_ms = duration
            self.source_duration_changed.emit(duration)
            self._resolve_trim(duration)
            emitted = self._playback_range.duration_ms if self._playback_range else duration
            self.duration_changed.emit(emitted)
            self._duration_emitted = True

        self._emit_state(self._map_state(obs_state))

        if self._playback_range is not None:
            if position >= self._playback_range.end_ms:
                source.media_play_pause(True)
                self.position_changed.emit(self._playback_range.duration_ms)
                self._emit_media_ended()
                return
            self.position_changed.emit(max(0, position - self._playback_range.start_ms))
        else:
            self.position_changed.emit(position)

        if obs_state == _OBS_STATE_ENDED:
            self._emit_media_ended()
        elif obs_state == _OBS_STATE_ERROR:
            self.error_occurred.emit("Playback failed.")
            self._poll.stop()

    def _resolve_trim(self, duration: int) -> None:
        request = self._request
        if request is None or request.trim is None or not request.trim.custom:
            return
        try:
            playback_range = request.trim.resolve(duration)
        except (TypeError, ValueError) as exc:
            self.error_occurred.emit(str(exc))
            self._lift_trim_mute()  # never leave the source stuck muted
            return
        self._playback_range = playback_range
        if self._source is not None:
            self._source.media_time = playback_range.start_ms
        # The seek to start_ms has landed — restore the user's volume/mute.
        self._lift_trim_mute()
        self.playback_range_changed.emit(playback_range.duration_ms, playback_range.start_ms)

    def _lift_trim_mute(self) -> None:
        if self._trim_muted:
            self._trim_muted = False
            self.audio_output.reapply()

    def _emit_media_ended(self) -> None:
        if self._ended_emitted:
            return
        self._ended_emitted = True
        self.media_ended.emit()

    def _emit_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if state != self._last_state:
            self._last_state = state
            self.state_changed.emit(state)

    @staticmethod
    def _map_state(obs_state: int) -> QMediaPlayer.PlaybackState:
        if obs_state == _OBS_STATE_PLAYING or obs_state == _OBS_STATE_BUFFERING:
            return QMediaPlayer.PlaybackState.PlayingState
        if obs_state == _OBS_STATE_PAUSED:
            return QMediaPlayer.PlaybackState.PausedState
        return QMediaPlayer.PlaybackState.StoppedState

    # ── Download callbacks ────────────────────────────────────────────────

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        self.buffer_progress.emit(downloaded, total)

    def _on_download_finished(self, local_path: str) -> None:
        # Switch from the streamed URL to the finished local file, preserving
        # playback position and state.
        if self._source is None or not self._current_url:
            return
        position = self._media_time()
        self._local_path = local_path
        self._teardown_source()
        self._play_local(local_path)
        if self._source is not None and position:
            self._source.media_time = position
        if self._cache_manager and self._stream_persist:
            self._cache_manager.notify_cached(self._current_url)
        self.playback_source_changed.emit(self._stream_persist)

    def _on_download_error(self, msg: str) -> None:
        log.warning("Downloader warning: %s", msg)
        if not MediaCacheManager.is_remote(self._current_url):
            return
        self.buffer_progress.emit(0, 0)
        if self._current_url:
            self.playback_download_failed.emit(self._current_url, msg, self._stream_persist)


__all__ = ["ObsMediaController"]
