"""MediaController — foreground media playback through the libobs sidecar.

libobs is the only media engine: every source (local, cached, or remote/streamed)
is decoded and composited by the sidecar's ``ffmpeg_source``, with audio routed to
the host via monitoring. There is no QtMultimedia in the playback path. This
controller adapts the app's transport API (play/pause/seek/volume/…) onto the
sidecar route and mirrors the sidecar's ``media_playback_state`` events back onto
the Qt signals the UI binds to.

Remote URLs stream through the sidecar directly; an already-cached copy is
preferred, and (per cache policy) a background download populates the cache for
offline reuse on the next play.
"""
import logging
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap  # noqa: F401 - part of the cover_art_changed contract

from .cache import MediaCacheManager
from .playback_state import (
    ENGINE_STATE_BUFFERING,
    ENGINE_STATE_ENDED,
    ENGINE_STATE_PAUSED,
    ENGINE_STATE_PLAYING,
    ENGINE_STATE_TO_SOLIN,
    SolinPlaybackState,
)
from .qt_contracts import PlaybackDownloaderFactory
from .playback_session import MediaPlaybackSession
from .playback_request import (
    TICKS_PER_MILLISECOND,
    MediaPlaybackRequest,
    PlaybackCachePolicy,
)
from .settings import MediaPlaybackSettings

log = logging.getLogger(__name__)


class MediaController(QObject):
    # SolinPlaybackState (Qt-free). Signal(object) because a Python enum is not a
    # registered Qt metatype; consumers compare against SolinPlaybackState members.
    state_changed     = Signal(object)
    duration_changed  = Signal(int)
    position_changed  = Signal(int)
    error_occurred    = Signal(str)
    media_ended       = Signal()
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

    def __init__(
        self,
        settings: MediaPlaybackSettings,
        cache_manager: MediaCacheManager,
        *,
        downloader_factory: PlaybackDownloaderFactory,
        metadata_extractor_factory: Callable[["QObject"], object] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache_manager = cache_manager
        # Retained only for cache lookups / background caching (get_cached_path,
        # start, cancel) — no Qt streaming or source-swap under the libobs engine.
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
        # True while a background cache copy is downloading behind a stream.
        self._caching_remote = False
        # True while the engine reports the stream re-establishing itself.
        self._engine_recovering = False

        # libobs sidecar route: the only decode path.
        self._engine_route = None
        self._engine_route_active = False
        self._engine_state = 0
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self._engine_media_state_received.connect(self._apply_engine_media_state)

        # Title + embedded cover come from ffprobe/ffmpeg out of band, since the
        # sidecar does not surface container tags.
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
    def is_playing(self) -> bool:
        return self._engine_route_active and self._engine_state == ENGINE_STATE_PLAYING

    @property
    def is_paused(self) -> bool:
        return self._engine_route_active and self._engine_state == ENGINE_STATE_PAUSED

    @property
    def duration(self) -> int:
        return self._engine_duration_ms if self._engine_route_active else 0

    @property
    def position(self) -> int:
        return self._engine_position_ms if self._engine_route_active else 0

    # ── Engine routing ───────────────────────────────────────────────────────

    def set_engine_media_route(self, route) -> None:
        """Install (or clear with ``None``) the libobs sidecar media route."""
        self._engine_route = route

    def _route_should_handle(self, url: str) -> bool:
        route = self._engine_route
        if route is None:
            return False
        is_ready = getattr(route, "is_ready", None)
        return is_ready() if callable(is_ready) else True

    def _volume_percent(self) -> int:
        return max(0, round(self._volume * 100))

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
        start_ms, end_trim_ms = self._trim_offsets(request)
        is_remote = MediaCacheManager.is_remote(url)
        self._engine_route_active = True
        self._engine_state = 0
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self._session.set_cached_local(url)
        self._session.set_stream_persist(False)
        self._engine_route.open(
            url,
            is_local_file=not is_remote,  # libobs ffmpeg_source streams remote URLs
            autoplay=request.autoplay,
            volume_percent=self._volume_percent(),
            speed_percent=self._speed_percent(),
            trim_start_ms=start_ms,
            trim_end_ms=end_trim_ms,
        )
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

    def on_engine_media_state(self, state) -> None:
        """Entry from the engine's event thread; hop to the GUI thread to apply."""
        self._engine_media_state_received.emit(state)

    def _apply_engine_media_state(self, state) -> None:
        if not self._engine_route_active:
            return
        duration = max(0, int(state.duration_ms))
        if duration != self._engine_duration_ms:
            self._engine_duration_ms = duration
            self.duration_changed.emit(duration)
        self._engine_position_ms = max(0, int(state.position_ms))
        self.position_changed.emit(self._engine_position_ms)
        raw_state = int(state.state)
        if raw_state != self._engine_state:
            self._engine_state = raw_state
            self.state_changed.emit(
                ENGINE_STATE_TO_SOLIN.get(raw_state, SolinPlaybackState.STOPPED)
            )
        # The sidecar reports a dropped stream as buffering rather than ended, so
        # this is the operator's cue that it is coming back rather than stuck.
        recovering = raw_state == ENGINE_STATE_BUFFERING
        if recovering != self._engine_recovering:
            self._engine_recovering = recovering
            self.playback_recovery_changed.emit(recovering)
        if raw_state == ENGINE_STATE_ENDED and not self._engine_media_ended_emitted:
            self._engine_media_ended_emitted = True
            self._session.mark_media_ended()
            self.media_ended.emit()
        error_code = getattr(state, "error_code", "")
        if error_code:
            self.error_occurred.emit(str(error_code))

    # ── Playback control ─────────────────────────────────────────────────────

    def start_playback(self, request: MediaPlaybackRequest) -> None:
        """Start one playback request through the libobs sidecar."""
        if not isinstance(request, MediaPlaybackRequest):
            raise TypeError("request must be a MediaPlaybackRequest")
        url = request.source
        self._request = request
        self._session.begin_playback(url, requested_playing=request.autoplay)
        self._engine_route_active = False

        if not self._route_should_handle(url):
            # libobs is the only engine; without a ready route there is nothing to
            # decode. Surface it rather than fail silently.
            log.warning("libobs engine route unavailable; cannot play %r", url)
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
        if self._engine_route_active:
            self._engine_route.play()

    def pause(self):
        self._session.set_requested_playing(False)
        if self._engine_route_active:
            self._engine_route.pause()

    def stop(self):
        self._metadata_extractor.cancel()
        self._caching_remote = False
        self._downloader.cancel()
        if not self._engine_route_active:
            return
        self._engine_route.close()
        self._engine_route_active = False
        self._session.begin_stop()
        self._session.finish_stop()
        self._request = None
        self._engine_state = 0
        self._engine_position_ms = 0
        self._engine_duration_ms = 0
        self._engine_media_ended_emitted = False
        self.state_changed.emit(SolinPlaybackState.STOPPED)
        self.playback_source_changed.emit(False)

    def toggle_play_pause(self):
        if self._engine_state == ENGINE_STATE_PLAYING:
            self.pause()
        else:
            self.play()

    def replay(self):
        """Restart from the effective beginning without replacing the source."""
        if self._engine_route_active:
            self._engine_media_ended_emitted = False
            self._session.set_requested_playing(True)
            self._engine_route.restart()

    def seek(self, ms: int):
        if self._engine_route_active:
            self._engine_route.seek(max(0, int(ms)))

    def set_volume(self, value: float):
        self._volume = max(0.0, min(1.0, float(value)))
        if self._engine_route_active:
            self._engine_route.set_properties(
                volume_percent=self._volume_percent(),
                speed_percent=self._speed_percent(),
            )

    def set_playback_rate(self, rate: float):
        self._playback_rate = max(0.1, rate)
        if self._engine_route_active:
            self._engine_route.set_properties(
                volume_percent=self._volume_percent(),
                speed_percent=self._speed_percent(),
            )
