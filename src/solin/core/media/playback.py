"""MediaController — foreground media playback through the libobs sidecar.

libobs is the only media engine: every source (local, cached, or remote/streamed)
is decoded and composited by the sidecar's ``ffmpeg_source``, with audio routed to
the host via monitoring. This controller adapts the app's transport API
(play/pause/seek/volume/…) onto the
sidecar route and mirrors the sidecar's ``media_playback_state`` events back onto
the Qt signals the UI binds to.

Remote URLs stream through the sidecar directly; an already-cached copy is
preferred, and (per cache policy) a background download populates the cache for
offline reuse on the next play.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap  # noqa: F401 - part of the cover_art_changed contract

from solin.core.scenes.media_control import MediaPlaybackNativeState, MediaPlaybackState

from .cache import MediaCacheManager
from .playback_state import ENGINE_STATE_TO_SOLIN, SolinPlaybackState
from .qt_contracts import MediaMetadataExtractor, MediaPlaybackRoute, PlaybackDownloaderFactory
from .playback_session import MediaPlaybackSession
from .playback_request import (
    TICKS_PER_MILLISECOND,
    MediaPlaybackRequest,
    PlaybackCachePolicy,
)
from .settings import MediaPlaybackSettings

if TYPE_CHECKING:
    from solin.core.scenes.engine import SceneEngineAck

log = logging.getLogger(__name__)


class MediaController(QObject):
    # SolinPlaybackState (Qt-free). Signal(object) because a Python enum is not a
    # registered Qt metatype; consumers compare against SolinPlaybackState members.
    state_changed     = Signal(object)
    playback_audio_expected_changed = Signal(bool)
    duration_changed  = Signal(int)
    position_changed  = Signal(int)
    error_occurred    = Signal(str)
    media_ended       = Signal()
    volume_changed    = Signal(float)
    cover_art_changed    = Signal(object)   # QPixmap | None
    title_from_metadata  = Signal(str)

    # True  -> a source is loaded and playing/paused; False -> stopped/cleared.
    playback_source_changed = Signal(bool)

    # (downloaded_bytes, total_bytes) of the background cache copy while a remote
    # item streams, so the transport can show how much is already on disk behind
    # the playback position.
    buffer_progress = Signal(int, int)

    # True while a stream is re-establishing itself after a dropped read, so the
    # transport can say so instead of looking frozen.
    playback_recovery_changed = Signal(bool)

    # Internal: a sidecar media_playback_state arrived on the engine's reader
    # thread; re-emitted here so the connected slot runs on the GUI thread
    # (AutoConnection → QueuedConnection), serialising engine state with the
    # GUI-thread transport methods that touch the same fields.
    _engine_media_state_received = Signal(object)
    _engine_open_finished = Signal(str, object)

    def __init__(
        self,
        settings: MediaPlaybackSettings,
        cache_manager: MediaCacheManager,
        *,
        downloader_factory: PlaybackDownloaderFactory,
        metadata_extractor_factory: Callable[[QObject], MediaMetadataExtractor] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache_manager = cache_manager
        # Cache lookups and background caching use get_cached_path/start/cancel;
        # the libobs sidecar plays the selected source.
        self._downloader = downloader_factory(self)
        # The cache download runs behind a streaming item; report it so the
        # transport can show what is already safely on disk.
        progress = getattr(self._downloader, "progress", None)
        if progress is not None and hasattr(progress, "connect"):
            progress.connect(self._on_cache_progress)
        self._session = MediaPlaybackSession()
        self._request: MediaPlaybackRequest | None = None
        self._playback_rate = 1.0
        self._volume = 1.0  # 0.0–1.0; forwarded to the sidecar as a percent
        self._audio_suppressed = False
        self._last_playback_audio_expected = False
        self._replacing_playback = False
        # True while a background cache copy is downloading behind a stream.
        self._caching_remote = False
        # True while the engine reports the stream re-establishing itself.
        self._engine_recovering = False

        # libobs sidecar route: the only decode path.
        self._engine_route: MediaPlaybackRoute | None = None
        self._engine_request_id: str | None = None
        self._engine_audio_silent = False
        self._engine_state: MediaPlaybackState | None = None
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self._engine_media_state_received.connect(self._apply_engine_media_state)
        self._engine_open_finished.connect(
            self._apply_engine_open_result, Qt.ConnectionType.QueuedConnection,
        )

        # Title and embedded cover come from the isolated media extractor,
        # since the sidecar does not surface container tags.
        if metadata_extractor_factory is None:
            from solin.core.media.routed_metadata import RoutedMediaMetadataExtractor

            metadata_extractor_factory = RoutedMediaMetadataExtractor
        self._metadata_extractor = metadata_extractor_factory(self)
        self._metadata_extractor.metadata_ready.connect(self._on_routed_metadata)

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def current_url(self) -> str:
        return self._session.current_url

    @property
    def current_occurrence_id(self) -> str:
        return self._request.occurrence_id if self._request is not None else ""

    @property
    def current_occurrence_container_id(self) -> str:
        return self._request.occurrence_container_id if self._request is not None else ""

    @property
    def local_path(self) -> str | None:
        return self._session.local_path

    @property
    def stream_persist(self) -> bool:
        return self._session.stream_persist

    @property
    def session_id(self) -> int:
        """Monotonic identifier for the currently loaded playback source."""
        return self._session.session_id

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def playback_rate(self) -> float:
        return self._playback_rate

    @property
    def is_playing(self) -> bool:
        return self._engine_route_active and self._engine_state == MediaPlaybackState.PLAYING

    @property
    def playback_audio_expected(self) -> bool:
        """Whether foreground transport can currently send audio to the room."""
        if self._replacing_playback:
            return self._last_playback_audio_expected
        if not self._engine_route_active or not self._output_volume_percent():
            return False
        if self._engine_state in (None, MediaPlaybackState.OPENING):
            # A newly opened source has yet to report its playback audio.
            return self._session.requested_playing and self._last_playback_audio_expected
        if self._engine_state == MediaPlaybackState.BUFFERING:
            return self._session.requested_playing
        return self.is_playing and not self._engine_audio_silent

    @property
    def is_paused(self) -> bool:
        return self._engine_route_active and self._engine_state == MediaPlaybackState.PAUSED

    @property
    def is_ended(self) -> bool:
        return self._engine_route_active and self._engine_state == MediaPlaybackState.ENDED

    @property
    def duration(self) -> int:
        return self._engine_duration_ms if self._engine_route_active else 0

    @property
    def position(self) -> int:
        return self._engine_position_ms if self._engine_route_active else 0

    # ── Engine routing ───────────────────────────────────────────────────────

    @property
    def _engine_route_active(self) -> bool:
        return self._engine_request_id is not None

    def set_engine_media_route(self, route: MediaPlaybackRoute | None) -> None:
        """Install (or clear with ``None``) the libobs sidecar media route."""
        self._engine_route = route

    def _route_should_handle(self, url: str) -> bool:
        route = self._engine_route
        if route is None:
            return False
        is_ready = getattr(route, "is_ready", None)
        return bool(is_ready()) if callable(is_ready) else True

    def _output_volume_percent(self) -> int:
        return 0 if self._audio_suppressed else round(self._volume * 100)

    def _publish_playback_audio_expected(self) -> None:
        expected = self.playback_audio_expected
        if expected != self._last_playback_audio_expected:
            self._last_playback_audio_expected = expected
            self.playback_audio_expected_changed.emit(expected)

    def _speed_percent(self) -> int:
        return max(1, round(self._playback_rate * 100))

    @staticmethod
    def _trim_offsets(request: MediaPlaybackRequest) -> tuple[int, int]:
        """(start_ms, end-trim ms) for the engine; the sidecar resolves the end."""
        trim = request.trim
        if trim is None or not trim.custom:
            return (0, 0)
        return (
            trim.start_trim_ticks // TICKS_PER_MILLISECOND,
            trim.end_trim_ticks // TICKS_PER_MILLISECOND,
        )

    def _begin_engine_playback(self, request: MediaPlaybackRequest, url: str) -> None:
        route = self._engine_route
        if route is None:
            raise RuntimeError("Media engine route is unavailable")
        start_ms, end_trim_ms = self._trim_offsets(request)
        is_remote = MediaCacheManager.is_remote(url)
        self._engine_state = None
        self._engine_audio_silent = False
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self._session.set_cached_local(url)
        self._session.set_stream_persist(False)
        request_id, completion = route.open(
            url,
            is_local_file=not is_remote,  # libobs ffmpeg_source streams remote URLs
            autoplay=request.autoplay,
            volume_percent=self._output_volume_percent(),
            speed_percent=self._speed_percent(),
            trim_start_ms=start_ms,
            trim_end_ms=end_trim_ms,
        )
        self._engine_request_id = request_id
        completion.add_done_callback(partial(self._engine_open_finished.emit, request_id))
        # The sidecar decodes the media; read its title + cover here for the UI.
        self._metadata_extractor.request(self._session.session_id, url)
        # The badge means "playing from a local copy", so it must follow the source.
        # Emitting True unconditionally lit it for streamed media too, telling the
        # operator a network item was safe to run offline.
        self.playback_source_changed.emit(not is_remote)

    def _on_routed_metadata(self, session_id: int, title: str, cover) -> None:
        """Apply a routed file's metadata.

        Guarded by ``session_id`` so a late read for a superseded playback is
        dropped rather than clobbering the current cover/title.
        """
        if not self._engine_route_active or session_id != self._session.session_id:
            return
        if title:
            self.title_from_metadata.emit(title)
        if cover is not None and not cover.isNull():
            self._session.mark_cover_emitted()
            self.cover_art_changed.emit(cover)
        elif not self._session.cover_emitted:
            self.cover_art_changed.emit(None)

    def on_engine_media_state(self, state: MediaPlaybackNativeState) -> None:
        """Entry from the engine's event thread; hop to the GUI thread to apply."""
        self._engine_media_state_received.emit(state)

    def _apply_engine_media_state(self, state: MediaPlaybackNativeState) -> None:
        if state.source_request_id != self._engine_request_id:
            return
        # Publish a coherent sample to duration and position listeners.
        engine_state = state.state
        state_changed = engine_state != self._engine_state
        self._engine_state = engine_state
        self._engine_audio_silent = state.audio_silent
        self._engine_position_ms = max(0, int(state.position_ms))
        if engine_state == MediaPlaybackState.PLAYING:
            # A restart uses the same source ID. Ignore queued EOF samples
            # until the decoder confirms that a new playback pass has started.
            self._engine_media_ended_emitted = False
        duration = max(0, int(state.duration_ms))
        if duration != self._engine_duration_ms:
            self._engine_duration_ms = duration
            self.duration_changed.emit(duration)
        self.position_changed.emit(self._engine_position_ms)
        if state_changed:
            self.state_changed.emit(
                ENGINE_STATE_TO_SOLIN.get(engine_state, SolinPlaybackState.STOPPED)
            )
        self._publish_playback_audio_expected()
        # Reflect the decoder's recovery state in the transport UI.
        recovering = engine_state == MediaPlaybackState.BUFFERING
        if recovering != self._engine_recovering:
            self._engine_recovering = recovering
            self.playback_recovery_changed.emit(recovering)
        if engine_state == MediaPlaybackState.ENDED and not self._engine_media_ended_emitted:
            self._engine_media_ended_emitted = True
            self._session.mark_media_ended()
            self.media_ended.emit()
        if state.error_code:
            self.error_occurred.emit(state.error_code)

    def _apply_engine_open_result(self, request_id: str, completion: Future[SceneEngineAck]) -> None:
        if (
            request_id != self._engine_request_id
            or self._engine_state not in (None, MediaPlaybackState.OPENING)
        ):
            return
        try:
            ack = completion.result()
        except Exception:  # noqa: BLE001 - IPC can fail before the sidecar sends a state
            log.debug("engine media open request failed", exc_info=True)
            error_code = "media_open_failed"
        else:
            if ack.applied:
                return
            error_code = ack.error_code
        self._apply_engine_media_state(MediaPlaybackNativeState(
            MediaPlaybackState.ERROR, self.position, self.duration,
            error_code=error_code, source_request_id=request_id,
        ))

    # ── Playback control ─────────────────────────────────────────────────────

    @contextmanager
    def replace_playback(self) -> Iterator[None]:
        """Stop the old transport while keeping its audio policy through a replacement."""
        replacing = self._replacing_playback
        self._replacing_playback = True
        try:
            self.stop()
            yield
        finally:
            self._replacing_playback = replacing
            self._publish_playback_audio_expected()

    def start_playback(self, request: MediaPlaybackRequest) -> None:
        """Start one playback request through the libobs sidecar."""
        if not isinstance(request, MediaPlaybackRequest):
            raise TypeError("request must be a MediaPlaybackRequest")
        url = request.source
        self._request = request
        self._session.begin_playback(url, requested_playing=request.autoplay)
        self._engine_request_id = None

        if not self._route_should_handle(url):
            # libobs is the only engine; without a ready route there is nothing to
            # decode. Surface it rather than fail silently.
            log.warning("libobs engine route unavailable; cannot play %r", url)
            self._publish_playback_audio_expected()
            self.error_occurred.emit("engine_unavailable")
            return

        target = url
        if MediaCacheManager.is_remote(url):
            cached = self._downloader.get_cached_path(url)
            if cached:
                self._caching_remote = False
                self._downloader.cancel()
                target = cached
            else:
                self._maybe_cache_remote(request, url)
        else:
            self._caching_remote = False
            self._downloader.cancel()
        self._begin_engine_playback(request, target)
        self._publish_playback_audio_expected()

    def _on_cache_progress(self, downloaded: int, total: int) -> None:
        """Forward the background cache download's progress to the transport.

        Only meaningful while a remote item streams: a local file has nothing
        downloading behind it, and reporting there would draw a buffer bar that
        never moves.
        """
        if self._caching_remote:
            self.buffer_progress.emit(int(downloaded), int(total))

    def _maybe_cache_remote(self, request: MediaPlaybackRequest, url: str) -> None:
        """Populate the cache in the background for offline reuse (no source-swap)."""
        if request.cache_policy is PlaybackCachePolicy.PROFILE_DEFAULT:
            persist = self._settings.auto_download_on_play()
        else:
            persist = request.cache_policy is PlaybackCachePolicy.PERSISTENT
        if persist:
            self._caching_remote = True
            self._downloader.start(url, persist=True)

    def play(self):
        self._session.set_requested_playing(True)
        if self._engine_route_active and self._engine_route is not None:
            self._publish_playback_audio_expected()
            self._engine_route.play()

    def pause(self):
        self._session.set_requested_playing(False)
        if self._engine_route_active and self._engine_route is not None:
            self._publish_playback_audio_expected()
            self._engine_route.pause()

    def stop(self):
        self._metadata_extractor.cancel()
        self._caching_remote = False
        self._downloader.cancel()
        if not self._engine_route_active and self._request is None:
            return
        if self._engine_route_active and self._engine_route is not None:
            self._engine_route.close()
        self._engine_request_id = None
        self._session.begin_stop()
        self._session.finish_stop()
        self._request = None
        self._engine_state = None
        self._engine_audio_silent = False
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self.state_changed.emit(SolinPlaybackState.STOPPED)
        self._publish_playback_audio_expected()
        self.playback_source_changed.emit(False)

    def toggle_play_pause(self):
        if self._engine_state == MediaPlaybackState.PLAYING or (
            self._engine_state == MediaPlaybackState.BUFFERING and self._session.requested_playing
        ):
            self.pause()
        else:
            self.play()

    def replay(self):
        """Restart from the effective beginning without replacing the source."""
        if self._engine_route_active and self._engine_route is not None:
            self._session.set_requested_playing(True)
            self._engine_route.restart()

    def seek(self, ms: int):
        if self._engine_route_active and self._engine_route is not None:
            self._engine_route.seek(max(0, int(ms)))

    def set_volume(self, value: float):
        previous = self._volume
        self._volume = max(0.0, min(1.0, float(value)))
        self._apply_playback_properties()
        self._publish_playback_audio_expected()
        if self._volume != previous:
            self.volume_changed.emit(self._volume)

    def set_audio_suppressed(self, suppressed: bool) -> None:
        """Temporarily silence output while keeping the configured volume."""
        if suppressed != self._audio_suppressed:
            self._audio_suppressed = suppressed
            self._apply_playback_properties()
            self._publish_playback_audio_expected()

    def set_playback_rate(self, rate: float):
        self._playback_rate = max(0.1, rate)
        self._apply_playback_properties()

    def _apply_playback_properties(self) -> None:
        if self._engine_route_active and self._engine_route is not None:
            self._engine_route.set_properties(
                volume_percent=self._output_volume_percent(),
                speed_percent=self._speed_percent(),
            )
