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
from PySide6.QtCore import QObject, Signal

from app.core.foundation import paths as _paths
from app.core.foundation.constants import TEMP_STREAM_PREFIX

PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.20
PROGRESS_EMIT_MIN_BYTES = 512 * 1024

log = logging.getLogger(__name__)


def _url_to_path(url: str) -> str:
    os.makedirs(_paths.MEDIA_CACHE_DIR, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0]
    return os.path.join(_paths.MEDIA_CACHE_DIR, filename)


def _make_temp_path(url: str) -> str:
    """Cria um arquivo temporário vazio com extensão correta e prefixo Solin_stream_."""
    name = url.split("/")[-1].split("?")[0]
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    fd, path = tempfile.mkstemp(suffix=ext, prefix=TEMP_STREAM_PREFIX)
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel: bool = False
        self._thread = None
        self._persist: bool = True
        self._lock = threading.Lock()
        self._writing_tmp = None   # .tmp em escrita (cleanup em cancel)
        self._finished_temp = None # temp entregue ao player (cleanup posterior)

    # -- API publica ----------------------------------------------------------

    def get_cached_path(self, url: str):
        """Retorna caminho local se arquivo persistente ja existe."""
        path = _url_to_path(url)
        if os.path.exists(path) and os.path.exists(path + ".done"):
            return path
        return None

    def start(self, url: str, persist: bool = True) -> None:
        """
        Inicia download em background.
        persist=True  -> salva permanentemente em MEDIA_CACHE_DIR
        persist=False -> salva em tempfile (apagado em cleanup_temp/cancel)
        """
        self.cancel()
        self._cancel  = False
        self._persist = persist
        self._thread  = threading.Thread(
            target=self._worker, args=(url, persist), daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """Cancela download ativo e remove arquivo .tmp de escrita."""
        self._cancel = True
        with self._lock:
            tmp = self._writing_tmp
            self._writing_tmp = None
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

    def _worker(self, url: str, persist: bool) -> None:
        if persist:
            final_path = _url_to_path(url)
            if os.path.exists(final_path) and os.path.exists(final_path + ".done"):
                size = os.path.getsize(final_path)
                self.progress.emit(size, size)
                self.finished.emit(final_path)
                return
            write_tmp = final_path + ".tmp"
        else:
            write_tmp  = _make_temp_path(url)
            final_path = write_tmp   # sem rename - temp e destino final
            _acquire_lock(write_tmp)  # registra imediatamente como "em uso"

        with self._lock:
            self._writing_tmp = write_tmp

        try:
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            total      = int(resp.headers.get("content-length", 0))
            downloaded = 0
            last_progress_at = 0.0
            last_progress_pct = -1
            last_progress_bytes = 0

            def emit_progress(force: bool = False) -> None:
                nonlocal last_progress_at, last_progress_pct, last_progress_bytes
                if downloaded <= 0:
                    return
                now = time.monotonic()
                if total > 0:
                    pct = int(downloaded * 100 / total)
                    if force or pct != last_progress_pct:
                        last_progress_pct = pct
                        last_progress_at = now
                        last_progress_bytes = downloaded
                        self.progress.emit(downloaded, total)
                    return
                if (
                    force
                    or last_progress_at <= 0
                    or downloaded - last_progress_bytes >= PROGRESS_EMIT_MIN_BYTES
                    or now - last_progress_at >= PROGRESS_EMIT_MIN_INTERVAL_SECONDS
                ):
                    last_progress_at = now
                    last_progress_bytes = downloaded
                    self.progress.emit(downloaded, total)

            with open(write_tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=131_072):
                    if self._cancel:
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        emit_progress()

            if self._cancel:
                return
            emit_progress(force=True)

            with self._lock:
                self._writing_tmp = None

            if persist:
                os.replace(write_tmp, final_path)
                with open(final_path + ".done", "w") as _f:
                    _f.write(url)
            else:
                with self._lock:
                    self._finished_temp = final_path

            self.finished.emit(final_path)

        except Exception as exc:
            with self._lock:
                self._writing_tmp = None
            if not self._cancel:
                _safe_remove(write_tmp)
                self.error.emit(str(exc))


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


def cleanup_incomplete_cache() -> int:
    """
    Varre MEDIA_CACHE_DIR em busca de arquivos de mídia sem marcador .done —
    resíduos de downloads interrompidos (crash, kill, queda de energia).

    Regra: para cada arquivo que NÃO termina em '.done' ou '.tmp',
    se não existir um '<arquivo>.done' ao lado, é download incompleto → apaga.
    Os próprios marcadores .done são preservados (são apenas sentinelas vazias).
    Os arquivos .tmp de escrita ativa também são removidos (nunca têm .done).

    Retorna o número de arquivos removidos.
    """
    if not os.path.isdir(_paths.MEDIA_CACHE_DIR):
        return 0

    removed = 0

    try:
        entries = os.listdir(_paths.MEDIA_CACHE_DIR)
    except OSError:
        return 0

    for name in entries:
        # Marcadores sentinela — nunca remover
        if name.endswith(".done"):
            continue

        path = os.path.join(_paths.MEDIA_CACHE_DIR, name)
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
