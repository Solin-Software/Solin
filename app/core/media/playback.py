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

from PySide6.QtCore import QObject, Signal, QUrl, QTimer
from app.core.foundation.settings_keys import SettingsKey
from app.core.profiles import settings as _ps  # noqa: F401 (used below)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame
from PySide6.QtGui import QPixmap, QImage

from .downloader import SongDownloader

log = logging.getLogger(__name__)


class MediaController(QObject):
    frame_ready       = Signal(QVideoFrame)
    state_changed     = Signal(QMediaPlayer.PlaybackState)
    duration_changed  = Signal(int)
    position_changed  = Signal(int)
    error_occurred    = Signal(str)
    media_ended       = Signal()
    cover_art_changed    = Signal(object)   # QPixmap | None
    title_from_metadata  = Signal(str)

    # (downloaded_bytes, total_bytes) — 0,0 quando nao ha download ativo
    buffer_progress   = Signal(int, int)

    # True  -> reproduzindo de arquivo local (offline/cache)
    # False -> reproduzindo via stream HTTP ou inativo
    playback_source_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

        self.video_sink = QVideoSink(self)
        self.player.setVideoSink(self.video_sink)

        self._downloader = SongDownloader(self)
        self._current_url: str = ""
        self._local_path: str | None = None
        # True quando _local_path e um tempfile (persist=False)
        self._local_is_temp: bool = False
        self._defer_local_switch: bool = False
        self._pending_local_switch: tuple[str, bool] | None = None
        self._pending_local_notified: bool = False
        self._cover_emitted: bool = False
        self._stream_persist: bool = True   # persiste no download atual?
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(800)
        self._reconnect_timer.timeout.connect(self._do_reconnect)

        self._session_id: int = 0
        self._frame_session: int = 0
        self._requested_playing: bool = False

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

    # ── Playback público ──────────────────────────────────────────────────

    def play_url(self, url: str):
        self._reconnect_timer.stop()

        # Limpa tempfile da faixa anterior (se houver) antes de iniciar nova
        self._cleanup_current_temp()

        self._current_url = url
        self._local_path = None
        self._local_is_temp = False
        self._pending_local_switch = None
        self._pending_local_notified = False
        self._cover_emitted = False
        self._requested_playing = True

        # Nova sessao — frames residuais do video anterior sao descartados
        self._session_id += 1
        self._frame_session = self._session_id

        # Cancela prefetch ativo para esta URL
        if url and url.startswith("http"):
            from .cache import MediaCacheManager
            MediaCacheManager.instance().cancel_prefetch(url)

        self.player.stop()
        self.player.setSource(QUrl())
        self.buffer_progress.emit(0, 0)

        import os
        cached = self._downloader.get_cached_path(url)
        if cached:
            self._local_path = cached
            self._local_is_temp = False
            size = os.path.getsize(cached)
            self.buffer_progress.emit(size, size)
            self._play_source(cached)
            self.playback_source_changed.emit(True)
        else:
            self._play_source(url)
            prefs = _ps.prefs()
            auto_download = prefs.value(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, True, bool)
            self._stream_persist = auto_download
            # Inicia download (persist ou temp) — buffer bar funciona em ambos
            self._downloader.start(url, persist=auto_download)
            self.playback_source_changed.emit(False)

    def play(self):
        self._requested_playing = True
        self.player.play()

    def pause(self):
        self._requested_playing = False
        self.player.pause()

    def stop(self):
        self._reconnect_timer.stop()
        self._downloader.cancel()
        self._session_id += 1
        self._requested_playing = False
        self.player.stop()
        self.player.setSource(QUrl())
        # Apaga tempfile se o player estiver usando um
        self._cleanup_current_temp()
        self._current_url = ""
        self._local_path = None
        self._local_is_temp = False
        self._defer_local_switch = False
        self._pending_local_switch = None
        self._pending_local_notified = False
        self.buffer_progress.emit(0, 0)
        self.playback_source_changed.emit(False)

    def toggle_play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def replay(self):
        """Reinicia do comeco sem limpar a fonte — usado no loop de item unico."""
        self.player.setPosition(0)
        self._requested_playing = True
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
        self._defer_local_switch = deferred
        if not deferred and self._pending_local_switch:
            local_path, local_is_temp = self._pending_local_switch
            already_notified = self._pending_local_notified
            self._pending_local_switch = None
            self._pending_local_notified = False
            self._switch_to_local(local_path, local_is_temp, notify_cache=not already_notified)

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
        is_network_drop = (
            "10054" in error_string
            or "10060" in error_string
            or "ConnectionReset" in error_string
            or "partial" in error_string.lower()
        )

        if is_network_drop and self._current_url:
            self._reconnect_timer.start()
        else:
            self.error_occurred.emit(error_string)

    def _do_reconnect(self):
        saved_pos = self.player.position()
        was_playing = self.is_playing or self._requested_playing
        source = self._local_path if self._local_path else self._current_url
        if not source:
            return

        self.player.stop()
        
        # Configura a nova fonte baseada no protocolo
        if source.startswith("http"):
            self.player.setSource(QUrl(source))
        else:
            self.player.setSource(QUrl.fromLocalFile(source))

        # Best practice: Reage ao estado da mídia ao invés de usar timer de polling
        def seek_on_ready(status):
            if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
                self.player.mediaStatusChanged.disconnect(seek_on_ready)
                self.player.setPosition(saved_pos)
                if was_playing:
                    self.player.play()
                else:
                    self.player.pause()
            
            elif status == QMediaPlayer.MediaStatus.InvalidMedia:
                # Previne memory leak se o source falhar
                self.player.mediaStatusChanged.disconnect(seek_on_ready)

        self.player.mediaStatusChanged.connect(seek_on_ready)

    # ── Download callbacks ────────────────────────────────────────────────

    def _on_download_progress(self, downloaded: int, total: int):
        self.buffer_progress.emit(downloaded, total)

    def _on_download_finished(self, local_path: str):
        self._local_path = local_path
        self._local_is_temp = not self._stream_persist

        if self._defer_local_switch:
            self._pending_local_switch = (local_path, self._local_is_temp)
            self._pending_local_notified = False
            if self._stream_persist and self._current_url:
                from .cache import MediaCacheManager
                MediaCacheManager.instance().notify_cached(self._current_url)
                self._pending_local_notified = True
            self.playback_source_changed.emit(self._stream_persist)
            return

        self._switch_to_local(local_path, self._local_is_temp)

    def _switch_to_local(self, local_path: str, local_is_temp: bool, notify_cache: bool = True):
        self._local_path = local_path
        self._local_is_temp = local_is_temp

        source = self.player.source().toString()
        if source.startswith("http") and self._current_url:
            # ── Captura o estado ANTES de tocar no player ─────────────────
            # Qualquer chamada ao player (stop, setSource) emite sinais síncronos
            # que podem perturbar a UI. Capturamos tudo antes.
            saved_pos   = self.player.position()
            was_playing = self.is_playing or self._requested_playing
            switch_session = self._session_id  # guard contra troca de mídia

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
                if self._session_id != switch_session:
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
        if notify_cache and self._stream_persist and self._current_url:
            from .cache import MediaCacheManager
            MediaCacheManager.instance().notify_cached(self._current_url)

        # Badge offline (ícone verde)
        self.playback_source_changed.emit(self._stream_persist)

    def _on_download_error(self, msg: str):
        log.warning("Downloader warning: %s", msg)

    # ── Player callbacks ──────────────────────────────────────────────────

    def _on_frame(self, frame):
        if self._session_id != self._frame_session:
            return
        self.frame_ready.emit(frame)

    def _on_state(self, state):
        self.state_changed.emit(state)

    def _on_duration(self, duration):
        self.duration_changed.emit(int(duration))

    def _on_position(self, position):
        self.position_changed.emit(int(position))

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._requested_playing = False
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
                    self._cover_emitted = True
                    self.cover_art_changed.emit(QPixmap.fromImage(img))
                    return
                if isinstance(img, QPixmap) and not img.isNull():
                    self._cover_emitted = True
                    self.cover_art_changed.emit(img)
                    return

        if not self._cover_emitted:
            self.cover_art_changed.emit(None)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _play_source(self, source: str):
        if source.startswith("http"):
            self.player.setSource(QUrl(source))
        else:
            self.player.setSource(QUrl.fromLocalFile(source))
        self.player.play()

    def _cleanup_current_temp(self) -> None:
        """
        Apaga o tempfile atual se o player estiver usando um.
        Cobre dois casos:
          (a) download ainda em andamento -> cancel() ja apaga o .tmp
          (b) download concluido, player usando tempfile -> cleanup_temp()
              apaga via _finished_temp; ou _local_path se ja foi movido
        """
        if self._local_is_temp and self._local_path:
            import os
            try:
                if os.path.isfile(self._local_path):
                    os.remove(self._local_path)
            except OSError:
                QTimer.singleShot(
                    250,
                    lambda path=self._local_path: self._remove_temp_later(path, 4),
                )
        else:
            # Download pode ter concluido mas ainda nao entregue a _local_path
            self._downloader.cleanup_temp()

    def _remove_temp_later(self, path: str, attempts_left: int) -> None:
        if not path:
            return
        import os
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            if attempts_left > 0:
                QTimer.singleShot(
                    250,
                    lambda: self._remove_temp_later(path, attempts_left - 1),
                )
