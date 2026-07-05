"""
SongDownloader — baixa mídia em background enquanto o player toca da URL.

Modos de operação
-----------------
persist=True  (padrão / download automático LIGADO)
    Salva em MEDIA_CACHE_DIR/<filename> + marcador .done
    Em cache -> re-usa na próxima reprodução

persist=False (download automático DESLIGADO)
    Salva em tempfile do SO (ex: /tmp/Solin_stream_XXXX.mp4)
    NENHUM arquivo permanente criado
    Mesmo sinal progress -> barra de buffer funciona igual
    Mesmo sinal finished -> player chaveia para arquivo local
    -> protegido contra queda de CDN, sem cache persistente
    Arquivo temp apagado via cleanup_temp() / cancel()
"""
import os
import logging
import threading
from dataclasses import dataclass
from PySide6.QtCore import QObject, Signal

from solin.core.network.http import stream_get
from .download_storage import (
    DownloadProgressGate,
    commit_persistent_download,
    completed_cached_path,
    prepare_download_target,
    safe_remove,
)

log = logging.getLogger(__name__)


@dataclass
class _DownloadJob:
    job_id: int
    url: str
    persist: bool
    cancel_event: threading.Event
    writing_tmp: str | None = None


class SongDownloader(QObject):
    """
    Sinais:
      progress(downloaded_bytes, total_bytes) - atualizacao de progresso
      finished(local_path)                    - download completo
      error(msg)                              - falha no download
    """
    progress = Signal(int, int)
    finished = Signal(str)
    error    = Signal(str)

    _worker_progress = Signal(int, int, int)
    _worker_finished = Signal(int, str, bool)
    _worker_error = Signal(int, str)

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._lock = threading.Lock()
        self._next_job_id = 0
        self._job: _DownloadJob | None = None
        self._thread: threading.Thread | None = None
        self._finished_temp: str | None = None
        self._worker_progress.connect(self._deliver_progress)
        self._worker_finished.connect(self._deliver_finished)
        self._worker_error.connect(self._deliver_error)
        self.destroyed.connect(lambda *_: self.cancel())

    # -- API publica ----------------------------------------------------------

    def get_cached_path(self, url: str):
        """Retorna caminho local se arquivo persistente ja existe."""
        return completed_cached_path(url, self._media_cache_dir)

    def start(self, url: str, persist: bool = True) -> int:
        """
        Inicia download em background.
        persist=True  -> salva permanentemente em MEDIA_CACHE_DIR
        persist=False -> salva em tempfile (apagado em cleanup_temp/cancel)
        """
        self.cancel()
        with self._lock:
            self._next_job_id += 1
            job = _DownloadJob(
                job_id=self._next_job_id,
                url=url,
                persist=persist,
                cancel_event=threading.Event(),
            )
            self._job = job
            thread = threading.Thread(
                target=self._worker,
                args=(job,),
                daemon=True,
                name=f"media-download-{job.job_id}",
            )
            self._thread = thread
        thread.start()
        return job.job_id

    def cancel(self) -> None:
        """Cancela o job atual e invalida todos os callbacks ainda enfileirados."""
        with self._lock:
            job = self._job
            self._job = None
            self._thread = None
            if job is not None:
                job.cancel_event.set()
                tmp = job.writing_tmp
                job.writing_tmp = None
            else:
                tmp = None
        safe_remove(tmp)

    def cleanup_temp(self) -> None:
        """
        Apaga o tempfile entregue ao player (persist=False).
        Deve ser chamado pelo MediaController em stop() e no playback seguinte.
        """
        with self._lock:
            path = self._finished_temp
            self._finished_temp = None
        safe_remove(path)

    def take_finished_temp(self):
        """Retorna (e limpa) o caminho do temp finalizado para rastreamento externo."""
        with self._lock:
            path = self._finished_temp
            self._finished_temp = None
        return path

    # -- Worker ---------------------------------------------------------------

    def _worker(self, job: _DownloadJob) -> None:
        url = job.url
        persist = job.persist
        target = prepare_download_target(
            url,
            self._media_cache_dir,
            persist=persist,
        )
        final_path = target.final_path
        write_tmp = target.write_path
        if target.is_cached:
            size = target.cached_size or 0
            try:
                self._worker_progress.emit(job.job_id, size, size)
                self._worker_finished.emit(job.job_id, final_path, True)
            except RuntimeError:
                return
            return

        with self._lock:
            job.writing_tmp = write_tmp

        succeeded = False
        try:
            if job.cancel_event.is_set():
                return
            with stream_get(url, timeout=30) as resp:
                total      = int(resp.headers.get("content-length", 0))
                downloaded = 0
                progress_gate = DownloadProgressGate()

                def emit_progress(force: bool = False) -> None:
                    if progress_gate.should_emit(
                        downloaded,
                        total,
                        force=force,
                        cancelled=job.cancel_event.is_set(),
                    ):
                        self._worker_progress.emit(job.job_id, downloaded, total)

                with open(write_tmp, "wb") as f:
                    for chunk in resp.iter_bytes(131_072):
                        if job.cancel_event.is_set():
                            return
                        f.write(chunk)
                        downloaded += len(chunk)
                        emit_progress()

            if job.cancel_event.is_set():
                return
            # The transport validates encoded transfer length before exposing chunks.
            emit_progress(force=True)

            if persist:
                with self._lock:
                    if (
                        self._job is not job
                        or job.cancel_event.is_set()
                    ):
                        return
                    commit_persistent_download(target, url)

            succeeded = True
            try:
                self._worker_finished.emit(job.job_id, final_path, persist)
            except RuntimeError:
                if not persist:
                    safe_remove(final_path)

        except Exception as exc:  # noqa: BLE001 - background download job boundary
            log.exception("Media download job %s failed", job.job_id)
            if not job.cancel_event.is_set():
                try:
                    self._worker_error.emit(job.job_id, str(exc))
                except RuntimeError:
                    log.debug("Downloader was destroyed before error delivery")
        finally:
            if not succeeded:
                with self._lock:
                    if job.writing_tmp == write_tmp:
                        job.writing_tmp = None
                safe_remove(write_tmp)

    def _is_current_job(self, job_id: int) -> bool:
        with self._lock:
            return self._job is not None and self._job.job_id == job_id

    def _deliver_progress(self, job_id: int, downloaded: int, total: int) -> None:
        if self._is_current_job(job_id):
            self.progress.emit(downloaded, total)

    def _deliver_finished(self, job_id: int, local_path: str, persist: bool) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                stale = True
            else:
                stale = False
                self._job.writing_tmp = None
                self._job = None
                self._thread = None
                if not persist:
                    self._finished_temp = local_path
        if stale:
            if not persist:
                safe_remove(local_path)
            return
        self.finished.emit(local_path)

    def _deliver_error(self, job_id: int, message: str) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                return
            self._job = None
            self._thread = None
        self.error.emit(message)
