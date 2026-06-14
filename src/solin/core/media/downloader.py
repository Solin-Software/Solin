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
import tempfile
import threading
import time
import requests
from dataclasses import dataclass
from PySide6.QtCore import QObject, Signal

from solin.core.foundation.constants import TEMP_STREAM_PREFIX

PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.20
PROGRESS_EMIT_MIN_BYTES = 512 * 1024

log = logging.getLogger(__name__)


def _url_to_path(url: str, media_cache_dir: str | os.PathLike[str]) -> str:
    cache_dir = os.fspath(media_cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0]
    return os.path.join(cache_dir, filename)


def _make_temp_path(url: str) -> str:
    """Cria um arquivo temporário vazio com extensão correta e prefixo Solin_stream_."""
    name = url.split("/")[-1].split("?")[0]
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    fd, path = tempfile.mkstemp(suffix=ext, prefix=TEMP_STREAM_PREFIX)
    os.close(fd)
    return path


def _make_persistent_temp_path(final_path: str) -> str:
    """Create a job-unique staging file beside the persistent destination."""
    directory = os.path.dirname(final_path)
    filename = os.path.basename(final_path)
    fd, path = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=directory,
    )
    os.close(fd)
    return path


def _lock_path(temp_path: str) -> str:
    """Retorna o caminho do lockfile correspondente ao tempfile."""
    return temp_path + ".lock"


def _acquire_lock(temp_path: str) -> None:
    """Cria o lockfile que sinaliza que este tempfile está em uso."""
    try:
        with open(_lock_path(temp_path), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _release_lock(temp_path: str) -> None:
    """Remove o lockfile ao deletar o tempfile."""
    try:
        lp = _lock_path(temp_path)
        if os.path.isfile(lp):
            os.remove(lp)
    except OSError:
        pass


def _safe_remove(path) -> None:
    """Remove o arquivo e seu lockfile (se existir)."""
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    # Sempre tenta remover o lock, mesmo se o arquivo já não existia
    if path:
        _release_lock(path)


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
        path = _url_to_path(url, self._media_cache_dir)
        if os.path.exists(path) and os.path.exists(path + ".done"):
            return path
        return None

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
        _safe_remove(tmp)

    def cleanup_temp(self) -> None:
        """
        Apaga o tempfile entregue ao player (persist=False).
        Deve ser chamado pelo MediaController em stop() e no play_url() seguinte.
        """
        with self._lock:
            path = self._finished_temp
            self._finished_temp = None
        _safe_remove(path)

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
        if persist:
            final_path = _url_to_path(url, self._media_cache_dir)
            if os.path.exists(final_path) and os.path.exists(final_path + ".done"):
                size = os.path.getsize(final_path)
                try:
                    self._worker_progress.emit(job.job_id, size, size)
                    self._worker_finished.emit(job.job_id, final_path, True)
                except RuntimeError:
                    return
                return
            write_tmp = _make_persistent_temp_path(final_path)
        else:
            write_tmp = _make_temp_path(url)
            final_path = write_tmp
            _acquire_lock(write_tmp)

        with self._lock:
            job.writing_tmp = write_tmp

        succeeded = False
        try:
            if job.cancel_event.is_set():
                return
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            total      = int(resp.headers.get("content-length", 0))
            downloaded = 0
            last_progress_at = 0.0
            last_progress_pct = -1
            last_progress_bytes = 0

            def emit_progress(force: bool = False) -> None:
                nonlocal last_progress_at, last_progress_pct, last_progress_bytes
                if downloaded <= 0 or job.cancel_event.is_set():
                    return
                now = time.monotonic()
                if total > 0:
                    pct = int(downloaded * 100 / total)
                    if force or pct != last_progress_pct:
                        last_progress_pct = pct
                        last_progress_at = now
                        last_progress_bytes = downloaded
                        self._worker_progress.emit(job.job_id, downloaded, total)
                    return
                if (
                    force
                    or last_progress_at <= 0
                    or downloaded - last_progress_bytes >= PROGRESS_EMIT_MIN_BYTES
                    or now - last_progress_at >= PROGRESS_EMIT_MIN_INTERVAL_SECONDS
                ):
                    last_progress_at = now
                    last_progress_bytes = downloaded
                    self._worker_progress.emit(job.job_id, downloaded, total)

            with open(write_tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=131_072):
                    if job.cancel_event.is_set():
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        emit_progress()

            if job.cancel_event.is_set():
                return
            # urllib3 validates the encoded transfer length before requests
            # transparently decodes Content-Encoding for iter_content().
            emit_progress(force=True)

            if persist:
                with self._lock:
                    if (
                        self._job is not job
                        or job.cancel_event.is_set()
                    ):
                        return
                    os.replace(write_tmp, final_path)
                    with open(final_path + ".done", "w") as marker:
                        marker.write(url)

            succeeded = True
            try:
                self._worker_finished.emit(job.job_id, final_path, persist)
            except RuntimeError:
                if not persist:
                    _safe_remove(final_path)

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
                _safe_remove(write_tmp)

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
                _safe_remove(local_path)
            return
        self.finished.emit(local_path)

    def _deliver_error(self, job_id: int, message: str) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                return
            self._job = None
            self._thread = None
        self.error.emit(message)


# ── Limpeza de órfãos na inicialização ────────────────────────────────────────

def cleanup_orphan_temps() -> int:
    """
    Varre o diretório de temporários do SO em busca de lockfiles órfãos
    com prefixo 'Solin_stream_' — sinal de que o app encerrou abruptamente
    sem deletar o tempfile correspondente.

    Filtra EXCLUSIVAMENTE pelo prefixo Solin_stream_ para não interferir
    em lockfiles de outros aplicativos no mesmo diretório.

    Retorna o número de arquivos removidos.
    """
    import tempfile as _tempfile

    tmp_dir = _tempfile.gettempdir()
    removed = 0

    try:
        entries = os.listdir(tmp_dir)
    except OSError:
        return 0

    for name in entries:
        # Interessa apenas lockfiles com prefixo Solin
        if not (name.startswith(TEMP_STREAM_PREFIX) and name.endswith(".lock")):
            continue

        lock_path = os.path.join(tmp_dir, name)
        # O tempfile tem o mesmo nome sem o sufixo ".lock"
        temp_path = lock_path[: -len(".lock")]

        # Remove tempfile (se ainda existir) e o lockfile
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
                removed += 1
            os.remove(lock_path)
        except OSError:
            pass  # Corrida improvável entre instâncias — ignora silenciosamente

    if removed:
        log.info("Removed %d orphan stream tempfile(s).", removed)

    return removed


def cleanup_incomplete_cache(
    media_cache_dir: str | os.PathLike[str],
) -> int:
    """
    Varre MEDIA_CACHE_DIR em busca de arquivos de mídia sem marcador .done —
    resíduos de downloads interrompidos (crash, kill, queda de energia).

    Regra: para cada arquivo que NÃO termina em '.done' ou '.tmp',
    se não existir um '<arquivo>.done' ao lado, é download incompleto → apaga.
    Os próprios marcadores .done são preservados (são apenas sentinelas vazias).
    Os arquivos .tmp de escrita ativa também são removidos (nunca têm .done).

    Retorna o número de arquivos removidos.
    """
    cache_dir = os.fspath(media_cache_dir)
    if not os.path.isdir(cache_dir):
        return 0

    removed = 0

    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return 0

    for name in entries:
        # Marcadores sentinela — nunca remover
        if name.endswith(".done"):
            continue

        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue

        # .tmp de escrita ativa ou arquivo de mídia sem .done → incompleto
        if name.endswith(".tmp") or not os.path.isfile(path + ".done"):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass

    if removed:
        log.info("Removed %d incomplete media cache download(s).", removed)

    return removed
