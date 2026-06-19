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
      Tempfile apagado no stop() ou no play_url() seguinte.
"""
import logging
import os

from PySide6.QtCore import QObject, Signal, QUrl, QTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
from PySide6.QtGui import QPixmap, QImage

from .cache import MediaCacheManager
from .qt_contracts import PlaybackDownloaderFactory
from .playback_session import MediaPlaybackSession
from .settings import MediaPlaybackSettings

log = logging.getLogger(__name__)


class MediaController(QObject):
    frame_ready       = Signal(QVideoFrame)
    state_changed     = Signal(QMediaPlayer.PlaybackState)
    duration_changed  = Signal(int)
    position_changed  = Signal(int)
    error_occurred    = Signal(str)
    playback_interrupted = Signal(str, str)  # url, message
    media_ended       = Signal()
    cover_art_changed    = Signal(object)   # QPixmap | None
    title_from_metadata  = Signal(str)
    playback_download_failed = Signal(str, str, bool)  # url, message, persist

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
        self._reconnect_timer.setInterval(800)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
        self._reconnect_attempts = 0
        self._reconnect_base_interval_ms = 800
        self._reconnect_max_interval_ms = 15000
        self._last_playback_error = ""
        self._last_known_position = 0
        self._stream_recovering = False
        self._handling_terminal_error = False

        self.video_sink.videoFrameChanged.connect(self._on_frame)
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
    def local_path(self) -> str | None:
        return self._session.local_path

    @property
    def stream_persist(self) -> bool:
        return self._session.stream_persist

    # ── Playback público ──────────────────────────────────────────────────

    def play_url(self, url: str, *, download_persist: bool | None = None):
        """Play a URL using the standard stream+cache pipeline.

        When ``download_persist`` is ``None``, the profile auto-download setting
        decides whether the parallel download is permanent or temporary.
        Callers such as background songs can force ``False`` to get the same
        resilient local-switch behavior without creating persistent cache files.
        """
        self._reconnect_timer.stop()
        self._reset_reconnect_state()

        # Limpa tempfile da faixa anterior (se houver) antes de iniciar nova
        self._cleanup_current_temp()

        self._session.begin_playback(url)
        self._last_known_position = 0

        # Cancela prefetch ativo para esta URL
        if MediaCacheManager.is_remote(url):
            self._cache_manager.cancel_prefetch(url)

        self.player.stop()
        self.player.setSource(QUrl())
        self.buffer_progress.emit(0, 0)

        cached = self._downloader.get_cached_path(url)
        if cached:
            self._session.set_cached_local(cached)
            self._session.set_stream_persist(True)
            size = os.path.getsize(cached)
            self.buffer_progress.emit(size, size)
            self._play_source(cached)
            self.playback_source_changed.emit(True)
        else:
            self._play_source(url)
            auto_download = (
                self._settings.auto_download_on_play()
                if download_persist is None else bool(download_persist)
            )
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
        self._reconnect_timer.stop()
        self._reset_reconnect_state()
        self._downloader.cancel()
        self._session.begin_stop()
        self.player.stop()
        self.player.setSource(QUrl())
        # Apaga tempfile se o player estiver usando um
        self._cleanup_current_temp()
        self._session.finish_stop()
        self._last_known_position = 0
        self.buffer_progress.emit(0, 0)
        self.playback_source_changed.emit(False)

    def toggle_play_pause(self):
        if (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            or (self._stream_recovering and self._session.requested_playing)
        ):
            self.pause()
        else:
            self.play()

    def replay(self):
        """Reinicia do comeco sem limpar a fonte — usado no loop de item unico."""
        self.player.setPosition(0)
        self._session.set_requested_playing(True)
        QTimer.singleShot(30, self.player.play)

    def seek(self, ms: int):
        self.player.setPosition(ms)

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
        return self.player.duration()

    @property
    def position(self) -> int:
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
            self._last_known_position > 0 or self.player.duration() > 0
        )
        is_network_drop = (
            error == QMediaPlayer.Error.NetworkError
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
        source = self._session.reconnect_source()
        if not source:
            self._fail_playback(self._last_playback_error)
            return

        reconnect_session = self._session.session_id
        self.player.stop()
        self.player.setSource(QUrl())
        self._set_player_source(source)
        self._restore_reconnect_position(
            source=source,
            saved_pos=saved_pos,
            reconnect_session=reconnect_session,
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
            saved_pos   = self.player.position()
            was_playing = self.is_playing or self._session.requested_playing
            switch_session = self._session.session_id  # guard contra troca de mídia

            # ── Troca a fonte sem chamar stop() explicitamente ────────────
            # setSource() já interrompe o stream HTTP internamente.
            # Chamar stop() antes emite PlaybackState.StoppedState de forma
            # síncrona, o que faz a UI (botão play/pause) piscar para "parado"
            # antes de o arquivo local estar pronto.
            self.player.setSource(QUrl.fromLocalFile(local_path))

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
                # Sessão mudou → nova mídia iniciada, abandona silenciosamente
                if self._session.session_id != switch_session:
                    return

                _attempts[0] += 1

                status   = self.player.mediaStatus()
                duration = self.player.duration()

                bad_statuses = (
                    QMediaPlayer.MediaStatus.InvalidMedia,
                    QMediaPlayer.MediaStatus.NoMedia,
                )
                if status in bad_statuses:
                    return  # falhou, não tenta mais

                # Considera pronto quando o player tem duração válida E o
                # status indica que os dados estão disponíveis para seek.
                ready_statuses = (
                    QMediaPlayer.MediaStatus.LoadedMedia,
                    QMediaPlayer.MediaStatus.BufferedMedia,
                )
                if status in ready_statuses and duration > 0:
                    # Seek seguro: restaura posição e estado de playback
                    self.player.setPosition(saved_pos)
                    if was_playing:
                        self.player.play()
                    return  # concluído, não agenda mais

                # Ainda carregando: tenta novamente se não excedeu o limite
                if _attempts[0] < _MAX_ATTEMPTS:
                    QTimer.singleShot(50, _try_restore)
                # else: timeout — o player ficará em StoppedState; o usuário
                # precisará pressionar play manualmente (caso extremamente raro)

            # Primeira tentativa após 50 ms (tempo para setSource processar)
            QTimer.singleShot(50, _try_restore)

        # Notifica CacheManager apenas em downloads persistentes
        if self._session.should_notify_cache(requested=notify_cache):
            self._cache_manager.notify_cached(self._session.current_url)

        # Badge offline (ícone verde)
        self.playback_source_changed.emit(self._session.stream_persist)

    def _on_download_error(self, msg: str):
        log.warning("Downloader warning: %s", msg)
        self.buffer_progress.emit(0, 0)
        if self._session.current_url:
            self.playback_download_failed.emit(
                self._session.current_url,
                msg,
                self._session.stream_persist,
            )

    # ── Player callbacks ──────────────────────────────────────────────────

    def _on_frame(self, frame):
        if not self._session.accepts_frame():
            return
        self.frame_ready.emit(frame)

    def _on_state(self, state):
        self.state_changed.emit(state)

    def _on_duration(self, duration):
        self.duration_changed.emit(int(duration))

    def _on_position(self, position):
        self._last_known_position = int(position)
        self.position_changed.emit(int(position))

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
        self._set_player_source(source)
        self.player.play()

    def _set_player_source(self, source: str) -> None:
        if MediaCacheManager.is_remote(source):
            self.player.setSource(QUrl(source))
        else:
            self.player.setSource(QUrl.fromLocalFile(source))

    def _restore_reconnect_position(
        self,
        *,
        source: str,
        saved_pos: int,
        reconnect_session: int,
    ) -> None:
        max_attempts = 60
        attempts = [0]

        def _try_restore() -> None:
            if self._session.session_id != reconnect_session:
                return

            attempts[0] += 1
            status = self.player.mediaStatus()
            duration = self.player.duration()

            if status == QMediaPlayer.MediaStatus.InvalidMedia:
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
                if saved_pos > 0:
                    self.player.setPosition(saved_pos)
                QTimer.singleShot(
                    40,
                    lambda: self._resume_after_reconnect(reconnect_session),
                )
                return

            if attempts[0] < max_attempts:
                QTimer.singleShot(50, _try_restore)
                return

            if self._session.requested_playing:
                self.player.play()
            else:
                self.player.pause()

        QTimer.singleShot(50, _try_restore)

    def _resume_after_reconnect(
        self,
        reconnect_session: int,
    ) -> None:
        if self._session.session_id != reconnect_session:
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
            self.playback_interrupted.emit(
                self._session.current_url,
                self._last_playback_error,
            )
        self._reconnect_attempts += 1
        exponent = min(self._reconnect_attempts - 1, 5)
        delay = min(
            self._reconnect_base_interval_ms * (2 ** exponent),
            self._reconnect_max_interval_ms,
        )
        self._reconnect_timer.setInterval(delay)
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
        self._reconnect_attempts = 0
        self._last_playback_error = ""
        self._stream_recovering = False

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
