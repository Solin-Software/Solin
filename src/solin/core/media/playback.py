"""
MediaController — controla QMediaPlayer com:
  - Download em background via SongDownloader (persist ou temp)
  - Auto-reconexao ao arquivo local se o CDN derrubar a conexao
  - Sinal buffer_progress para a barra de buffer do seek slider

Comportamento de buffer por modo
─────────────────────────────────
  Download ON  (persist=True)
      Player stream HTTP + SongDownloader grava MEDIA_CACHE_DIR
      Barra de buffer mostra progresso; ao terminar, player chaveía
      para arquivo local permanente.

  Download OFF (persist=False)
      Player stream HTTP + SongDownloader grava TEMPFILE
      Barra de buffer mostra progresso igual ao modo ON
      Ao terminar, player chaveía para tempfile → protegido contra
      queda de CDN, mas nenhum arquivo permanente fica em disco.
      Tempfile apagado no stop() ou no playback seguinte.
"""
import logging
import os

from PySide6.QtCore import QMetaObject, QObject, Signal, Slot, QUrl, QTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
from PySide6.QtGui import QPixmap, QImage

from .cache import MediaCacheManager
from .qt_contracts import PlaybackDownloaderFactory
from .playback_session import MediaPlaybackSession
from .playback_request import (
    MediaPlaybackRequest,
    PlaybackCachePolicy,
    ResolvedPlaybackRange,
)
from .settings import MediaPlaybackSettings

log = logging.getLogger(__name__)


class MediaController(QObject):
    frame_ready       = Signal(QVideoFrame)
    decoded_frame_acceptance_changed = Signal(int, bool)
    state_changed     = Signal(QMediaPlayer.PlaybackState)
    duration_changed  = Signal(int)
    source_duration_changed = Signal(int)
    position_changed  = Signal(int)
    error_occurred    = Signal(str)
    playback_interrupted = Signal(str, str)  # url, message
    playback_recovery_changed = Signal(bool)
    media_ended       = Signal()
    cover_art_changed    = Signal(object)   # QPixmap | None
    title_from_metadata  = Signal(str)
    playback_download_failed = Signal(str, str, bool)  # url, message, persist
    playback_range_changed = Signal(int, int)  # relative duration, absolute start

    # (downloaded_bytes, total_bytes) — 0,0 quando nao ha download ativo
    buffer_progress   = Signal(int, int)

    # True  -> reproduzindo de arquivo local (offline/cache)
    # False -> reproduzindo via stream HTTP ou inativo
    playback_source_changed = Signal(bool)

    def __init__(
        self,
        settings: MediaPlaybackSettings,
        cache_manager: MediaCacheManager,
        *,
        downloader_factory: PlaybackDownloaderFactory,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache_manager = cache_manager

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

        self.video_sink = QVideoSink(self)
        self.player.setVideoSink(self.video_sink)

        self._downloader = downloader_factory(self)
        self._session = MediaPlaybackSession()
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(3000)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
        self._reconnect_attempts = 0
        self._reconnect_interval_ms = 3000
        self._last_playback_error = ""
        self._last_known_position = 0
        self._remote_playback_started = False
        self._stream_recovering = False
        self._handling_terminal_error = False
        self._request: MediaPlaybackRequest | None = None
        self._playback_range: ResolvedPlaybackRange | None = None
        self._trim_gate_open = True
        self._trim_end_emitted = False
        self._gate_previous_muted = False
        self._audio_gate_active = False
        self._source_generation = 0
        self._published_frame_acceptance: tuple[int, bool] | None = None
        self._python_frame_delivery_required = True
        self._python_frame_connection: QMetaObject.Connection | None = None

        self._connect_python_frame_delivery()
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.metaDataChanged.connect(self._on_metadata_changed)

        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.finished.connect(self._on_download_finished)
        self._downloader.error.connect(self._on_download_error)

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
        return self.audio_output.volume()

    @property
    def is_recovering(self) -> bool:
        return self._stream_recovering

    @property
    def python_frame_delivery_required(self) -> bool:
        return self._python_frame_delivery_required

    @Slot(bool)
    def set_python_frame_delivery_required(self, required: bool) -> None:
        required = bool(required)
        if required == self._python_frame_delivery_required:
            return
        self._python_frame_delivery_required = required
        if required:
            self._connect_python_frame_delivery()
            self._on_frame(self.video_sink.videoFrame())
            return
        connection = self._python_frame_connection
        self._python_frame_connection = None
        if connection is not None:
            QObject.disconnect(connection)

    def _connect_python_frame_delivery(self) -> None:
        if self._python_frame_connection is not None:
            return
        self._python_frame_connection = self.video_sink.videoFrameChanged.connect(
            self._on_frame
        )

    # ── Playback público ──────────────────────────────────────────────────

    def start_playback(self, request: MediaPlaybackRequest) -> None:
        """Start one immutable playback request.

        Trimmed playback is fail-closed: output remains gated until the source
        reports a usable duration, supports seeking, and confirms the requested
        initial position. A remote stream is never downloaded in full merely to
        satisfy a trim request.
        """
        if not isinstance(request, MediaPlaybackRequest):
            raise TypeError("request must be a MediaPlaybackRequest")
        url = request.source
        self._reconnect_timer.stop()
        self._reset_reconnect_state()

        # Limpa tempfile da faixa anterior (se houver) antes de iniciar nova.
        # start() cancela download antigo; fontes locais/cacheadas nao chamam start().
        self._cleanup_current_temp()

        self._request = request
        self._playback_range = None
        self._trim_end_emitted = False
        self._trim_gate_open = request.trim is None or not request.trim.custom
        self._session.begin_playback(url, requested_playing=request.autoplay)
        self._publish_decoded_frame_acceptance()
        self._last_known_position = 0
        self._remote_playback_started = False

        # Cancela prefetch ativo para esta URL
        is_remote = MediaCacheManager.is_remote(url)
        if is_remote:
            self._cache_manager.cancel_prefetch(url)

        self.player.stop()
        self._clear_player_source()
        self._restore_gated_audio()
        self.buffer_progress.emit(0, 0)

        if not is_remote:
            self._downloader.cancel()
            self._session.set_cached_local(url)
            self._session.set_stream_persist(False)
            self._play_source(url)
            self.playback_source_changed.emit(True)
            return

        cached = self._downloader.get_cached_path(url)
        if cached:
            self._downloader.cancel()
            self._session.set_cached_local(cached)
            self._session.set_stream_persist(True)
            size = os.path.getsize(cached)
            self.buffer_progress.emit(size, size)
            self._play_source(cached)
            self.playback_source_changed.emit(True)
        else:
            self._play_source(url)
            if request.cache_policy is PlaybackCachePolicy.PROFILE_DEFAULT:
                auto_download = self._settings.auto_download_on_play()
            else:
                auto_download = request.cache_policy is PlaybackCachePolicy.PERSISTENT
            self._session.set_stream_persist(auto_download)
            # Inicia download (persist ou temp) — buffer bar funciona em ambos
            self._downloader.start(url, persist=auto_download)
            self.playback_source_changed.emit(False)

    def play(self):
        self._session.set_requested_playing(True)
        self.player.play()

    def pause(self):
        self._session.set_requested_playing(False)
        self.player.pause()

    def stop(self):
        was_gated = not self._trim_gate_open
        self._reconnect_timer.stop()
        self._reset_reconnect_state()
        self._downloader.cancel()
        self._session.begin_stop()
        self._publish_decoded_frame_acceptance()
        self.player.stop()
        self._clear_player_source()
        # Apaga tempfile se o player estiver usando um
        self._cleanup_current_temp()
        self._session.finish_stop()
        self._last_known_position = 0
        self._remote_playback_started = False
        self._request = None
        self._playback_range = None
        self._trim_gate_open = True
        self._trim_end_emitted = False
        self._restore_gated_audio()
        self.buffer_progress.emit(0, 0)
        self.playback_source_changed.emit(False)
        if was_gated:
            self.state_changed.emit(self.player.playbackState())

    def toggle_play_pause(self):
        if (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            or (self._stream_recovering and self._session.requested_playing)
        ):
            self.pause()
        else:
            self.play()

    def replay(self):
        """Restart from the effective beginning without replacing the source."""
        start_ms = self._playback_range.start_ms if self._playback_range else 0
        self._trim_end_emitted = False
        self._session.set_requested_playing(True)
        if self._playback_range is None:
            playback_session = self._session.session_id
            source_generation = self._source_generation
            self.player.setPosition(0)

            def play_if_current() -> None:
                if (
                    self._session.session_id == playback_session
                    and self._source_generation == source_generation
                    and self._session.requested_playing
                ):
                    self.player.play()

            QTimer.singleShot(30, play_if_current)
            return
        self._gate_output()
        self.player.pause()
        self.player.setPosition(start_ms)
        self._confirm_trimmed_start(
            self._session.session_id,
            self._source_generation,
            self._playback_range,
        )

    def seek(self, ms: int):
        relative_ms = max(0, int(ms))
        if self._playback_range is None:
            self.player.setPosition(relative_ms)
            return
        absolute_ms = self._playback_range.start_ms + min(
            relative_ms,
            self._playback_range.duration_ms,
        )
        if absolute_ms < self._playback_range.end_ms:
            self._trim_end_emitted = False
        self.player.setPosition(absolute_ms)

    def set_local_switch_deferred(self, deferred: bool) -> None:
        """
        Pausa a troca stream HTTP -> arquivo local.

        Usado pelo Song Announcement Mode: durante o gate, trocar a fonte do
        QMediaPlayer reinicia/perturba posição e estado, então a pausa precisa
        ocorrer primeiro. Depois aplicamos a troca usando a posição atual.
        """
        request = self._session.set_local_switch_deferred(deferred)
        if request is not None:
            self._switch_to_local(
                request.local_path,
                request.local_is_temp,
                notify_cache=request.notify_cache,
            )

    def set_volume(self, value: float):
        self.audio_output.setVolume(value)

    def set_playback_rate(self, rate: float):
        self.player.setPlaybackRate(max(0.1, rate))

    @property
    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def is_paused(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState

    @property
    def duration(self) -> int:
        if self._playback_range is not None:
            return self._playback_range.duration_ms
        return self.player.duration()

    @property
    def position(self) -> int:
        if self._playback_range is not None:
            return max(
                0,
                min(
                    self.player.position() - self._playback_range.start_ms,
                    self._playback_range.duration_ms,
                ),
            )
        return self.player.position()

    # ── Reconexao automatica ──────────────────────────────────────────────

    def _on_error(self, error, error_string: str):
        error_detail = (error_string or "").strip()
        lowered = error_detail.lower()
        if "immediate exit requested" in lowered:
            return
        source = self.player.source().toString()
        if not self._session.current_url and not source:
            return
        is_remote_playback = MediaCacheManager.is_remote(self._session.current_url)
        started_remote_playback = is_remote_playback and (
            self._remote_playback_started
            or self._last_known_position > 0
            or self.player.duration() > 0
        )
        is_network_drop = (
            (is_remote_playback and self._stream_recovering)
            or error == QMediaPlayer.Error.NetworkError
            or (started_remote_playback and error != QMediaPlayer.Error.NoError)
            or (
                "10054" in error_detail
                or "10060" in error_detail
                or "connectionreset" in lowered
                or "partial" in lowered
                or "unable to read from socket" in lowered
                or ("i/o error" in lowered and MediaCacheManager.is_remote(source))
            )
        )

        if is_network_drop and self._session.current_url:
            self._schedule_reconnect(error_detail)
        else:
            self._fail_playback(error_detail)

    def _do_reconnect(self):
        saved_pos = max(self.player.position(), self._last_known_position)
        if self._has_active_trim_request():
            self._gate_output()
        if self._playback_range is not None:
            saved_pos = max(
                self._playback_range.start_ms,
                min(saved_pos, self._playback_range.end_ms - 1),
            )
        source = self._session.reconnect_source()
        if not source:
            self._fail_playback(self._last_playback_error)
            return

        reconnect_session = self._session.session_id
        self.player.stop()
        self._clear_player_source()
        self._set_player_source(source)
        reconnect_generation = self._source_generation
        self._restore_reconnect_position(
            source=source,
            saved_pos=saved_pos,
            reconnect_session=reconnect_session,
            source_generation=reconnect_generation,
        )

    # ── Download callbacks ────────────────────────────────────────────────

    def _on_download_progress(self, downloaded: int, total: int):
        self.buffer_progress.emit(downloaded, total)

    def _on_download_finished(self, local_path: str):
        decision = self._session.download_finished(local_path)

        if decision.deferred:
            if decision.notify_cache_now:
                self._cache_manager.notify_cached(self._session.current_url)
            self.playback_source_changed.emit(self._session.stream_persist)
            return

        request = decision.switch_request
        if request is not None:
            self._switch_to_local(request.local_path, request.local_is_temp)

    def _switch_to_local(self, local_path: str, local_is_temp: bool, notify_cache: bool = True):
        self._session.mark_local_switch(local_path, local_is_temp)

        source = self.player.source().toString()
        if source.startswith("http") and self._session.current_url:
            # ── Captura o estado ANTES de tocar no player ─────────────────
            # Qualquer chamada ao player (stop, setSource) emite sinais síncronos
            # que podem perturbar a UI. Capturamos tudo antes.
            saved_pos = self.player.position()
            switch_session = self._session.session_id  # guard contra troca de mídia
            playback_range = self._playback_range
            if self._has_active_trim_request():
                self._gate_output()
            if playback_range is not None:
                saved_pos = max(
                    playback_range.start_ms,
                    min(saved_pos, playback_range.end_ms - 1),
                )

            # ── Troca a fonte sem chamar stop() explicitamente ────────────
            # setSource() já interrompe o stream HTTP internamente.
            # Chamar stop() antes emite PlaybackState.StoppedState de forma
            # síncrona, o que faz a UI (botão play/pause) piscar para "parado"
            # antes de o arquivo local estar pronto.
            self._set_player_source(local_path)
            switch_generation = self._source_generation

            # ── Restaura posição e estado via polling com retry ────────────
            # A abordagem de callback (mediaStatusChanged) é frágil porque:
            #   • LoadedMedia é emitido antes do seek ser seguro em alguns backends
            #   • BufferingMedia não garante que setPosition funciona
            #   • O mesmo sinal é compartilhado com _on_status (EndOfMedia), gerando
            #     interferência entre listeners
            # A solução robusta é um timer de polling que verifica se o player
            # está em estado seekable (duration > 0) antes de restaurar.
            _MAX_ATTEMPTS = 40   # 40 × 50 ms = 2 s de timeout máximo
            _attempts = [0]

            def _try_restore():
                nonlocal playback_range, saved_pos
                # Sessão mudou → nova mídia iniciada, abandona silenciosamente
                if (
                    self._session.session_id != switch_session
                    or self._source_generation != switch_generation
                ):
                    return

                _attempts[0] += 1

                status   = self.player.mediaStatus()
                duration = self.player.duration()

                bad_statuses = (
                    QMediaPlayer.MediaStatus.InvalidMedia,
                    QMediaPlayer.MediaStatus.NoMedia,
                )
                if status in bad_statuses:
                    self._fail_trimmed_preparation(
                        "The downloaded media could not be opened."
                    )
                    return

                # Considera pronto quando o player tem duração válida E o
                # status indica que os dados estão disponíveis para seek.
                ready_statuses = (
                    QMediaPlayer.MediaStatus.LoadedMedia,
                    QMediaPlayer.MediaStatus.BufferedMedia,
                )
                if status in ready_statuses and duration > 0:
                    if self._has_active_trim_request() and playback_range is None:
                        self._prepare_trimmed_source(
                            switch_session,
                            switch_generation,
                        )
                        return
                    if playback_range is not None:
                        try:
                            playback_range = self._resolve_active_range(duration)
                        except ValueError as exc:
                            self._fail_trimmed_preparation(str(exc))
                            return
                        self._playback_range = playback_range
                        saved_pos = max(
                            playback_range.start_ms,
                            min(saved_pos, playback_range.end_ms - 1),
                        )
                    # Seek seguro: restaura posição e estado de playback
                    self.player.setPosition(saved_pos)
                    if playback_range is not None:
                        self._confirm_local_handoff(
                            switch_session,
                            switch_generation,
                            saved_pos,
                            playback_range,
                        )
                    elif self._session.requested_playing:
                        self.player.play()
                    return  # concluído, não agenda mais

                # Ainda carregando: tenta novamente se não excedeu o limite
                if _attempts[0] < _MAX_ATTEMPTS:
                    QTimer.singleShot(50, _try_restore)
                    return
                self._fail_trimmed_preparation(
                    "Timed out while opening the downloaded media."
                )

            # Primeira tentativa após 50 ms (tempo para setSource processar)
            QTimer.singleShot(50, _try_restore)

        # Notifica CacheManager apenas em downloads persistentes
        if self._session.should_notify_cache(requested=notify_cache):
            self._cache_manager.notify_cached(self._session.current_url)

        # Badge offline (ícone verde)
        self.playback_source_changed.emit(self._session.stream_persist)

    def _confirm_local_handoff(
        self,
        playback_session: int,
        source_generation: int,
        target_position: int,
        playback_range: ResolvedPlaybackRange,
    ) -> None:
        attempts = [0]

        def confirm() -> None:
            if (
                self._session.session_id != playback_session
                or self._source_generation != source_generation
            ):
                return
            attempts[0] += 1
            if abs(self.player.position() - target_position) <= 150:
                self._trim_gate_open = True
                self._publish_decoded_frame_acceptance()
                self._restore_gated_audio()
                self.duration_changed.emit(playback_range.duration_ms)
                self.playback_range_changed.emit(
                    playback_range.duration_ms,
                    playback_range.start_ms,
                )
                if self._session.requested_playing:
                    self.player.play()
                else:
                    self.player.pause()
                self.state_changed.emit(self.player.playbackState())
                return
            if attempts[0] < 80:
                QTimer.singleShot(50, confirm)
                return
            self._fail_trimmed_preparation(
                "The downloaded media did not confirm the playback position."
            )

        QTimer.singleShot(25, confirm)

    def _on_download_error(self, msg: str):
        log.warning("Downloader warning: %s", msg)
        if not MediaCacheManager.is_remote(self._session.current_url):
            return
        self.buffer_progress.emit(0, 0)
        if self._session.current_url:
            self.playback_download_failed.emit(
                self._session.current_url,
                msg,
                self._session.stream_persist,
            )

    # ── Player callbacks ──────────────────────────────────────────────────

    def _on_frame(self, frame):
        if not self._trim_gate_open or not self._session.accepts_frame():
            return
        self.frame_ready.emit(frame)

    def _on_state(self, state):
        if not self._trim_gate_open:
            return
        self.state_changed.emit(state)

    def _on_duration(self, duration):
        if MediaCacheManager.is_remote(self._session.current_url) and duration > 0:
            self._remote_playback_started = True
        self.source_duration_changed.emit(int(duration))
        if self._request and self._request.trim and self._request.trim.custom:
            if self._playback_range is None:
                return
            duration = self._playback_range.duration_ms
        self.duration_changed.emit(int(duration))

    def _on_position(self, position):
        position_int = int(position)
        if (
            self._stream_recovering
            and position_int <= 0
            and self._last_known_position > 0
        ):
            return
        if MediaCacheManager.is_remote(self._session.current_url) and position_int > 0:
            self._remote_playback_started = True
        self._last_known_position = position_int
        if not self._trim_gate_open:
            return
        playback_range = self._playback_range
        if playback_range is None:
            self.position_changed.emit(position_int)
            return
        if position_int >= playback_range.end_ms:
            self._gate_output()
            self.player.pause()
            self.player.setPosition(playback_range.end_ms)
            self.position_changed.emit(playback_range.duration_ms)
            if not self._trim_end_emitted:
                self._trim_end_emitted = True
                self._session.mark_media_ended()
                self.media_ended.emit()
            return
        relative_position = max(0, position_int - playback_range.start_ms)
        self.position_changed.emit(relative_position)

    def _on_status(self, status):
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._reset_reconnect_state()
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._stream_recovering:
                return
            if self._is_unexpected_remote_end():
                self._schedule_reconnect("The media stream was interrupted.")
                return
            if not self._trim_end_emitted:
                self._trim_end_emitted = True
                self._session.mark_media_ended()
                self.media_ended.emit()

    def _on_metadata_changed(self):
        from PySide6.QtMultimedia import QMediaMetaData
        meta = self.player.metaData()

        title_val = meta.value(QMediaMetaData.Key.Title)
        if title_val and isinstance(title_val, str) and title_val.strip():
            self.title_from_metadata.emit(title_val.strip())

        for key in (QMediaMetaData.Key.CoverArtImage, QMediaMetaData.Key.ThumbnailImage):
            value = meta.value(key)
            if value is not None:
                img = value
                if isinstance(img, QImage) and not img.isNull():
                    self._session.mark_cover_emitted()
                    self.cover_art_changed.emit(QPixmap.fromImage(img))
                    return
                if isinstance(img, QPixmap) and not img.isNull():
                    self._session.mark_cover_emitted()
                    self.cover_art_changed.emit(img)
                    return

        if not self._session.cover_emitted:
            self.cover_art_changed.emit(None)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _play_source(self, source: str):
        request = self._request
        if request and request.trim and request.trim.custom:
            self._gate_output()
        self._set_player_source(source)
        if request and request.trim and request.trim.custom:
            self._prepare_trimmed_source(
                self._session.session_id,
                self._source_generation,
            )
        elif request is None or request.autoplay:
            self.player.play()
        else:
            self.player.pause()

    def _gate_output(self) -> None:
        if not self._audio_gate_active:
            self._gate_previous_muted = self.audio_output.isMuted()
            self.audio_output.setMuted(True)
            self._audio_gate_active = True
        self._trim_gate_open = False
        self._publish_decoded_frame_acceptance()

    @property
    def decoded_frames_accepted(self) -> bool:
        return bool(
            self._session.current_url
            and self._trim_gate_open
            and self._session.accepts_frame()
        )

    def _publish_decoded_frame_acceptance(self) -> None:
        state = (self._session.session_id, self.decoded_frames_accepted)
        if state == self._published_frame_acceptance:
            return
        self._published_frame_acceptance = state
        self.decoded_frame_acceptance_changed.emit(*state)

    def _restore_gated_audio(self) -> None:
        if not self._audio_gate_active:
            return
        self.audio_output.setMuted(self._gate_previous_muted)
        self._audio_gate_active = False

    def _prepare_trimmed_source(
        self,
        playback_session: int,
        source_generation: int,
    ) -> None:
        """Load, validate and seek a bounded source before exposing output."""
        max_ready_attempts = 200  # 10 seconds
        ready_attempts = [0]
        self.player.play()

        def wait_until_seekable() -> None:
            if (
                self._session.session_id != playback_session
                or self._source_generation != source_generation
            ):
                return
            ready_attempts[0] += 1
            status = self.player.mediaStatus()
            if status in (
                QMediaPlayer.MediaStatus.InvalidMedia,
                QMediaPlayer.MediaStatus.NoMedia,
            ):
                self._fail_trimmed_preparation(
                    "The media could not be prepared for custom start and end times."
                )
                return

            if status in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            ) and self.player.duration() > 0:
                if not self.player.isSeekable():
                    if ready_attempts[0] < max_ready_attempts:
                        QTimer.singleShot(50, wait_until_seekable)
                        return
                    self._fail_trimmed_preparation(
                        "This media source does not support reliable seeking. "
                        "Download it for offline use or remove the custom times."
                    )
                    return
                request = self._request
                if request is None or request.trim is None:
                    return
                try:
                    playback_range = request.trim.resolve(self.player.duration())
                except (TypeError, ValueError) as exc:
                    self._fail_trimmed_preparation(str(exc))
                    return
                self._playback_range = playback_range
                self.player.pause()
                self.player.setPosition(playback_range.start_ms)
                self._confirm_trimmed_start(
                    playback_session,
                    source_generation,
                    playback_range,
                )
                return

            if ready_attempts[0] < max_ready_attempts:
                QTimer.singleShot(50, wait_until_seekable)
                return
            self._fail_trimmed_preparation(
                "Timed out while preparing custom start and end times."
            )

        QTimer.singleShot(0, wait_until_seekable)

    def _confirm_trimmed_start(
        self,
        playback_session: int,
        source_generation: int,
        playback_range: ResolvedPlaybackRange,
    ) -> None:
        attempts = [0]
        max_attempts = 80  # 4 seconds after metadata is available

        def confirm() -> None:
            if (
                self._session.session_id != playback_session
                or self._source_generation != source_generation
            ):
                return
            attempts[0] += 1
            position = self.player.position()
            tolerance_ms = 150
            if (
                playback_range.start_ms <= position
                <= playback_range.start_ms + tolerance_ms
            ):
                self._trim_gate_open = True
                self._publish_decoded_frame_acceptance()
                self._restore_gated_audio()
                self._trim_end_emitted = False
                self.duration_changed.emit(playback_range.duration_ms)
                self.position_changed.emit(0)
                self.playback_range_changed.emit(
                    playback_range.duration_ms,
                    playback_range.start_ms,
                )
                if self._session.requested_playing:
                    self.player.play()
                else:
                    self.player.pause()
                self.state_changed.emit(self.player.playbackState())
                return
            if attempts[0] < max_attempts:
                if attempts[0] % 10 == 0:
                    self.player.setPosition(playback_range.start_ms)
                QTimer.singleShot(50, confirm)
                return
            self._fail_trimmed_preparation(
                "The media source did not confirm the custom start position."
            )

        QTimer.singleShot(25, confirm)

    def _fail_trimmed_preparation(self, detail: str) -> None:
        if not self._session.current_url:
            return
        message = (detail or "").strip() or "The media could not be prepared."
        self.stop()
        self.error_occurred.emit(message)

    def _resolve_active_range(self, duration: int) -> ResolvedPlaybackRange:
        request = self._request
        if request is None or request.trim is None:
            raise ValueError("Custom playback times are no longer available.")
        return request.trim.resolve(duration)

    def _has_active_trim_request(self) -> bool:
        request = self._request
        return bool(request and request.trim and request.trim.custom)

    def _set_player_source(self, source: str) -> None:
        self._source_generation += 1
        if MediaCacheManager.is_remote(source):
            self.player.setSource(QUrl(source))
        else:
            self.player.setSource(QUrl.fromLocalFile(source))

    def _clear_player_source(self) -> None:
        self._source_generation += 1
        self.player.setSource(QUrl())

    def _restore_reconnect_position(
        self,
        *,
        source: str,
        saved_pos: int,
        reconnect_session: int,
        source_generation: int,
    ) -> None:
        max_attempts = 60
        attempts = [0]

        def _try_restore() -> None:
            nonlocal saved_pos
            if (
                self._session.session_id != reconnect_session
                or self._source_generation != source_generation
            ):
                return

            attempts[0] += 1
            status = self.player.mediaStatus()
            duration = self.player.duration()

            if status in (
                QMediaPlayer.MediaStatus.InvalidMedia,
                QMediaPlayer.MediaStatus.NoMedia,
            ):
                if MediaCacheManager.is_remote(source) and self._session.current_url:
                    self._schedule_reconnect(self._last_playback_error)
                else:
                    self._fail_playback(self._last_playback_error)
                return

            ready_statuses = (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
                QMediaPlayer.MediaStatus.BufferingMedia,
                QMediaPlayer.MediaStatus.StalledMedia,
            )
            if status in ready_statuses and (duration > 0 or attempts[0] >= 8):
                playback_range = self._playback_range
                if self._has_active_trim_request() and playback_range is None:
                    self._prepare_trimmed_source(
                        reconnect_session,
                        source_generation,
                    )
                    return
                if playback_range is not None:
                    try:
                        playback_range = self._resolve_active_range(duration)
                    except ValueError as exc:
                        self._fail_trimmed_preparation(str(exc))
                        return
                    self._playback_range = playback_range
                    saved_pos = max(
                        playback_range.start_ms,
                        min(saved_pos, playback_range.end_ms - 1),
                    )
                if saved_pos > 0:
                    self.player.setPosition(saved_pos)
                if playback_range is not None:
                    self._confirm_local_handoff(
                        reconnect_session,
                        source_generation,
                        saved_pos,
                        playback_range,
                    )
                    return
                QTimer.singleShot(
                    40,
                    lambda: self._resume_after_reconnect(
                        reconnect_session,
                        source_generation,
                    ),
                )
                return

            if attempts[0] < max_attempts:
                QTimer.singleShot(50, _try_restore)
                return

            if MediaCacheManager.is_remote(source) and self._session.current_url:
                self._schedule_reconnect(self._last_playback_error)
                return

            if self._session.requested_playing:
                self.player.play()
            else:
                self.player.pause()

        QTimer.singleShot(50, _try_restore)

    def _resume_after_reconnect(
        self,
        reconnect_session: int,
        source_generation: int,
    ) -> None:
        if (
            self._session.session_id != reconnect_session
            or self._source_generation != source_generation
        ):
            return
        if self._session.requested_playing:
            self.player.play()
        else:
            self.player.pause()

    def _schedule_reconnect(self, error_detail: str) -> None:
        self._last_playback_error = (
            error_detail or "The media stream was interrupted."
        )
        if not self._stream_recovering:
            self._stream_recovering = True
            self.playback_recovery_changed.emit(True)
            self.playback_interrupted.emit(
                self._session.current_url,
                self._last_playback_error,
            )
        self._reconnect_attempts += 1
        self._reconnect_timer.setInterval(self._reconnect_interval_ms)
        self._reconnect_timer.start()

    def _fail_playback(self, error_detail: str) -> None:
        if self._handling_terminal_error:
            return
        self._handling_terminal_error = True
        try:
            detail = (error_detail or "").strip() or "The media could not be opened."
            self.error_occurred.emit(detail)
            if self._session.current_url:
                self.stop()
        finally:
            self._handling_terminal_error = False

    def _reset_reconnect_state(self) -> None:
        was_recovering = self._stream_recovering
        self._reconnect_attempts = 0
        self._last_playback_error = ""
        self._stream_recovering = False
        if was_recovering:
            self.playback_recovery_changed.emit(False)

    def _is_unexpected_remote_end(self) -> bool:
        if not MediaCacheManager.is_remote(self._session.current_url):
            return False
        duration = self.player.duration()
        position = max(self.player.position(), self._last_known_position)
        if duration <= 0:
            return False
        return position + 1500 < duration

    def _cleanup_current_temp(self) -> None:
        """
        Apaga o tempfile atual se o player estiver usando um.
        Cobre dois casos:
          (a) download ainda em andamento -> cancel() ja apaga o .tmp
          (b) download concluido, player usando tempfile -> cleanup_temp()
              apaga via _finished_temp; ou local_path se ja foi movido
        """
        temp_path = self._session.current_temp_path()
        if temp_path:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                QTimer.singleShot(
                    250,
                    lambda path=temp_path: self._remove_temp_later(path, 4),
                )
        else:
            # Download pode ter concluido mas ainda nao entregue a local_path
            self._downloader.cleanup_temp()

    def _remove_temp_later(self, path: str, attempts_left: int) -> None:
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            if attempts_left > 0:
                QTimer.singleShot(
                    250,
                    lambda: self._remove_temp_later(path, attempts_left - 1),
                )
