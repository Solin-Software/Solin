"""
MediaCacheManager — gerencia cache e pré-download de mídia remota.

O manager centraliza a fila global de prefetch para que ações em lote não
criem dezenas de SongDownloader simultâneos. UIs consultam estado ativo/queued
separadamente: ativo mostra progresso; queued mostra espera sem spinner falso.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import time
from typing import Any, Callable, Protocol, cast
from PySide6.QtCore import QObject, Signal, Slot

MAX_CONCURRENT_PREFETCHES = 3
PREFETCH_RETRY_LIMIT = 1
MAX_BATCH_PREPROGRESS_FAILURES = 3
PREFETCH_PROGRESS_MIN_INTERVAL_SECONDS = 0.25
PREFETCH_PROGRESS_MIN_BYTES = 512 * 1024

# ── Helpers de path (espelham downloader.py para evitar import circular) ──────

def _url_to_filename(url: str) -> str:
    return url.split("/")[-1].split("?")[0]


def cached_path_for(url: str, media_cache_dir: str | os.PathLike[str]) -> str:
    """Retorna o caminho local esperado para a URL (arquivo pode não existir)."""
    cache_dir = os.fspath(media_cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, _url_to_filename(url))


def is_url_cached(url: str, media_cache_dir: str | os.PathLike[str]) -> bool:
    """True se o arquivo local existe E o marcador .done está presente."""
    if not url or not url.startswith("http"):
        return False
    try:
        path = cached_path_for(url, media_cache_dir)
        return os.path.isfile(path) and os.path.isfile(path + ".done")
    except (OSError, ValueError):
        return False


class _Downloader(Protocol):
    progress: Any
    finished: Any
    error: Any

    def cancel(self) -> None: ...

    def start(self, url: str) -> None: ...


@dataclass
class _QueuedPrefetch:
    url: str
    batch_id: str = ""
    retries: int = 0


@dataclass
class _ActivePrefetch:
    downloader: _Downloader
    batch_id: str = ""
    retries: int = 0
    had_progress: bool = False
    last_progress_emit_at: float = 0.0
    last_progress_pct: int = -1
    last_progress_bytes: int = 0


@dataclass
class _BatchState:
    total: int = 0
    queued: set[str] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    done: int = 0
    failed: int = 0
    consecutive_preprogress_failures: int = 0
    canceled: bool = False


class MediaCacheManager(QObject):
    """Owns the application media-cache queue on the Qt main thread."""

    _notify_cached_requested = Signal(str)
    # url que agora está em cache (por prefetch ou notificação do player)
    cache_changed      = Signal(str)
    # caminho local removido do cache (arquivo principal; .done também é removido)
    cache_removed      = Signal(str)
    # progresso do prefetch: (url, bytes_baixados, bytes_total)
    prefetch_progress  = Signal(str, int, int)
    # erro no prefetch: (url, mensagem)
    prefetch_error     = Signal(str, str)
    # url aguardando uma vaga na fila
    prefetch_queued    = Signal(str)
    # url saiu da fila pendente (iniciou, cancelou ou foi descartada)
    prefetch_dequeued  = Signal(str)
    # status de lote: batch_id, queued, active, done, failed
    prefetch_batch_changed = Signal(str, int, int, int, int)
    # erro de lote emitido uma vez quando o restante e abortado
    prefetch_batch_error = Signal(str, str)

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.media_cache_dir = Path(media_cache_dir)
        self.max_concurrent_prefetches = MAX_CONCURRENT_PREFETCHES
        self._queue: deque[_QueuedPrefetch] = deque()
        self._queued: dict[str, _QueuedPrefetch] = {}
        self._active: dict[str, _ActivePrefetch] = {}
        self._batches: dict[str, _BatchState] = {}
        self._downloader_factory: Callable[[QObject], _Downloader] | None = None
        self._batch_signal_suppressed = 0
        self._dirty_batches: set[str] = set()
        self._notify_cached_requested.connect(self.notify_cached)

    # ── API pública ───────────────────────────────────────────────────────

    def is_cached(self, url: str) -> bool:
        return is_url_cached(url, self.media_cache_dir)

    @staticmethod
    def is_remote(url: str) -> bool:
        """True se a URL é HTTP/HTTPS (portanto sujeita a cacheamento)."""
        return bool(url and url.startswith("http"))

    def is_prefetching(self, url: str) -> bool:
        """True se há um prefetch ativo para a URL."""
        return url in self._active

    def is_queued(self, url: str) -> bool:
        """True se a URL aguarda uma vaga na fila de prefetch."""
        return url in self._queued

    @Slot(str)
    def prefetch(self, url: str, priority: bool = False) -> None:
        """
        Inicia download em background para pré-cachear a mídia.
        No-op se já cacheado; se a fila estiver cheia, aguarda uma vaga.
        """
        self._enqueue(url, priority=priority)
        self._pump_queue()

    def prefetch_many(self, urls: list[str], batch_id: str) -> int:
        """Enfileira um lote deduplicado e retorna quantas URLs entraram na fila."""
        if not batch_id:
            return 0

        added = 0
        seen: set[str] = set()
        batch = self._batches.setdefault(batch_id, _BatchState())
        batch.canceled = False

        self._batch_signal_suppressed += 1
        try:
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                if self._enqueue(url, batch_id=batch_id, priority=False):
                    added += 1
                    batch.total += 1
        finally:
            self._batch_signal_suppressed = max(0, self._batch_signal_suppressed - 1)
        self._dirty_batches.add(batch_id)
        self._flush_deferred_batch_signals()
        self._pump_queue()
        return added

    def batch_counts(self, batch_id: str) -> tuple[int, int, int, int]:
        batch = self._batches.get(batch_id)
        if not batch:
            return 0, 0, 0, 0
        return len(batch.queued), len(batch.active), batch.done, batch.failed

    def batch_is_active(self, batch_id: str) -> bool:
        batch = self._batches.get(batch_id)
        return bool(batch and (batch.queued or batch.active))

    def _enqueue(
        self,
        url: str,
        *,
        batch_id: str = "",
        priority: bool = False,
        retries: int = 0,
    ) -> bool:
        if not self.is_remote(url):
            return False
        if self.is_cached(url):
            self.cache_changed.emit(url)
            return False
        if url in self._active:
            return False
        if url in self._queued:
            if priority:
                self._promote_queued(url)
            return False

        entry = _QueuedPrefetch(url=url, batch_id=batch_id, retries=retries)
        if priority:
            self._queue.appendleft(entry)
        else:
            self._queue.append(entry)
        self._queued[url] = entry

        if batch_id:
            batch = self._batches.setdefault(batch_id, _BatchState())
            batch.queued.add(url)
            self._emit_batch_changed(batch_id)

        self.prefetch_queued.emit(url)
        return True

    def cancel_prefetch(self, url: str) -> None:
        """
        Cancela o prefetch ativo para a URL (se houver).
        Deve ser chamado pelo MediaController ANTES de iniciar seu download,
        para evitar gravações simultâneas no mesmo .tmp.
        """
        queued = self._queued.pop(url, None)
        if queued is not None:
            self._queue = deque(entry for entry in self._queue if entry.url != url)
            self._remove_from_batch(queued.batch_id, url, was_queued=True)
            self.prefetch_dequeued.emit(url)
            return

        active = self._active.pop(url, None)
        if active is not None:
            self._remove_from_batch(active.batch_id, url, was_active=True)
            active.downloader.cancel()
            self.prefetch_dequeued.emit(url)
            self._pump_queue()

    def cancel_all(self) -> None:
        """Cancela todos os prefetches ativos (ex: ao fechar o app)."""
        queued_urls = list(self._queued.keys())
        for url in queued_urls:
            self.cancel_prefetch(url)
        for url in list(self._active.keys()):
            self.cancel_prefetch(url)

    def cancel_batch(self, batch_id: str) -> None:
        batch = self._batches.get(batch_id)
        if not batch:
            return
        batch.canceled = True
        for url in list(batch.queued):
            self.cancel_prefetch(url)
        for url in list(batch.active):
            self.cancel_prefetch(url)
        self._emit_batch_changed(batch_id)

    def _promote_queued(self, url: str) -> None:
        entry = self._queued.get(url)
        if entry is None:
            return
        self._queue = deque(item for item in self._queue if item.url != url)
        self._queue.appendleft(entry)
        self.prefetch_queued.emit(url)

    def notify_cached(self, url: str) -> None:
        """
        Chamado pelo MediaController quando SEU download termina.
        Garante que o prefetch concorrente (se houvesse) seja removido do dict
        e emite cache_changed para atualizar a UI.
        """
        active = self._active.pop(url, None)
        if active is not None:
            self._mark_batch_done(active.batch_id, url)
        queued = self._queued.pop(url, None)
        if queued is not None:
            self._queue = deque(entry for entry in self._queue if entry.url != url)
            self._remove_from_batch(queued.batch_id, url, was_queued=True)
            self.prefetch_dequeued.emit(url)
        if url:
            self.cache_changed.emit(url)
        self._pump_queue()

    def notify_cached_threadsafe(self, url: str) -> None:
        """Queue a cache notification onto the manager's owning Qt thread."""
        self._notify_cached_requested.emit(url)

    def remove_cached_file(self, path: str) -> bool:
        """
        Remove um arquivo do cache e seu marcador .done, emitindo cache_removed.

        Retorna True se o arquivo principal ou o marcador foram removidos.
        Centralizar esse fluxo evita UIs com estado stale após exclusão manual.
        """
        if not path:
            return False

        removed = False
        for target in (path, path + ".done"):
            try:
                if os.path.isfile(target):
                    os.remove(target)
                    removed = True
            except OSError:
                raise

        if removed:
            self.cache_removed.emit(path)
        return removed

    # ── Slots internos ────────────────────────────────────────────────────

    def _create_downloader(self) -> _Downloader:
        if self._downloader_factory is not None:
            return self._downloader_factory(self)

        # Import lazy para evitar circular dependency no topo do módulo.
        from .downloader import SongDownloader
        from PySide6.QtCore import QCoreApplication

        dl = SongDownloader(self.media_cache_dir, self)
        app = QCoreApplication.instance()
        if app is not None:
            dl.moveToThread(app.thread())
        return cast(_Downloader, dl)

    def _pump_queue(self) -> None:
        while self._queue and len(self._active) < self.max_concurrent_prefetches:
            entry = self._queue.popleft()
            if self._queued.pop(entry.url, None) is None:
                continue
            if self.is_cached(entry.url):
                self._mark_batch_done(entry.batch_id, entry.url)
                self.cache_changed.emit(entry.url)
                self.prefetch_dequeued.emit(entry.url)
                continue
            if entry.url in self._active:
                continue
            self._start_entry(entry)

    def _start_entry(self, entry: _QueuedPrefetch) -> None:
        dl = self._create_downloader()
        dl.progress.connect(
            lambda d, t, u=entry.url: self._on_prefetch_progress(u, d, t)
        )
        dl.finished.connect(
            lambda path, u=entry.url: self._on_prefetch_done(u)
        )
        dl.error.connect(
            lambda msg, u=entry.url: self._on_prefetch_error(u, msg)
        )

        self._active[entry.url] = _ActivePrefetch(
            downloader=dl,
            batch_id=entry.batch_id,
            retries=entry.retries,
        )
        self.prefetch_dequeued.emit(entry.url)

        if entry.batch_id:
            batch = self._batches.setdefault(entry.batch_id, _BatchState())
            batch.queued.discard(entry.url)
            batch.active.add(entry.url)
            self._emit_batch_changed(entry.batch_id)

        dl.start(entry.url)

    def _on_prefetch_progress(self, url: str, downloaded: int, total: int) -> None:
        active = self._active.get(url)
        if active is not None and downloaded > 0:
            active.had_progress = True
            batch = self._batches.get(active.batch_id)
            if batch is not None:
                batch.consecutive_preprogress_failures = 0
        if active is not None and not self._should_emit_progress(active, downloaded, total):
            return
        self.prefetch_progress.emit(url, downloaded, total)

    def _on_prefetch_done(self, url: str) -> None:
        active = self._active.pop(url, None)
        if active is not None:
            self._mark_batch_done(active.batch_id, url)
        self.cache_changed.emit(url)
        self._pump_queue()

    def _on_prefetch_error(self, url: str, msg: str) -> None:
        active = self._active.pop(url, None)
        if active is None:
            return

        self._remove_from_batch(active.batch_id, url, was_active=True)

        if self._is_fatal_cache_error(msg):
            self._mark_batch_failed(active.batch_id, url, preprogress=not active.had_progress)
            self.prefetch_error.emit(url, msg)
            if active.batch_id:
                self._abort_batch(active.batch_id, msg)
            self._pump_queue()
            return

        if active.retries < PREFETCH_RETRY_LIMIT:
            self._enqueue(
                url,
                batch_id=active.batch_id,
                priority=True,
                retries=active.retries + 1,
            )
            self._pump_queue()
            return

        self._mark_batch_failed(active.batch_id, url, preprogress=not active.had_progress)
        self.prefetch_error.emit(url, msg)
        self._pump_queue()

    def _mark_batch_done(self, batch_id: str, url: str) -> None:
        if not batch_id:
            return
        batch = self._batches.setdefault(batch_id, _BatchState())
        batch.queued.discard(url)
        batch.active.discard(url)
        batch.done += 1
        batch.consecutive_preprogress_failures = 0
        self._emit_batch_changed(batch_id)

    def _mark_batch_failed(self, batch_id: str, url: str, *, preprogress: bool) -> None:
        if not batch_id:
            return
        batch = self._batches.setdefault(batch_id, _BatchState())
        batch.queued.discard(url)
        batch.active.discard(url)
        batch.failed += 1
        if preprogress:
            batch.consecutive_preprogress_failures += 1
        else:
            batch.consecutive_preprogress_failures = 0
        self._emit_batch_changed(batch_id)
        if batch.consecutive_preprogress_failures >= MAX_BATCH_PREPROGRESS_FAILURES:
            self._abort_batch(
                batch_id,
                "Several downloads failed before receiving data. Check your connection.",
            )

    def _remove_from_batch(
        self,
        batch_id: str,
        url: str,
        *,
        was_queued: bool = False,
        was_active: bool = False,
    ) -> None:
        if not batch_id:
            return
        batch = self._batches.get(batch_id)
        if not batch:
            return
        if was_queued:
            batch.queued.discard(url)
        if was_active:
            batch.active.discard(url)
        self._emit_batch_changed(batch_id)

    def _abort_batch(self, batch_id: str, msg: str) -> None:
        batch = self._batches.get(batch_id)
        if not batch or batch.canceled:
            return
        batch.canceled = True
        for queued_url in list(batch.queued):
            queued = self._queued.pop(queued_url, None)
            if queued is not None:
                self._queue = deque(entry for entry in self._queue if entry.url != queued_url)
                self.prefetch_dequeued.emit(queued_url)
        batch.queued.clear()
        for active_url in list(batch.active):
            active = self._active.pop(active_url, None)
            if active is not None:
                active.downloader.cancel()
                self.prefetch_dequeued.emit(active_url)
        batch.active.clear()
        self.prefetch_batch_error.emit(batch_id, msg)
        self._emit_batch_changed(batch_id)

    def _emit_batch_changed(self, batch_id: str) -> None:
        if not batch_id:
            return
        if self._batch_signal_suppressed > 0:
            self._dirty_batches.add(batch_id)
            return
        queued, active, done, failed = self.batch_counts(batch_id)
        self.prefetch_batch_changed.emit(batch_id, queued, active, done, failed)

    def _flush_deferred_batch_signals(self) -> None:
        if self._batch_signal_suppressed > 0:
            return
        dirty = list(self._dirty_batches)
        self._dirty_batches.clear()
        for batch_id in dirty:
            self._emit_batch_changed(batch_id)

    @staticmethod
    def _is_fatal_cache_error(msg: str) -> bool:
        lowered = (msg or "").lower()
        fatal_markers = (
            "no space",
            "not enough space",
            "disk full",
            "quota",
            "permission denied",
            "access is denied",
            "winerror 112",
            "errno 28",
            "errno 13",
        )
        return any(marker in lowered for marker in fatal_markers)

    @staticmethod
    def _should_emit_progress(active: _ActivePrefetch, downloaded: int, total: int) -> bool:
        now = time.monotonic()
        if total > 0:
            pct = int(downloaded * 100 / total)
            if pct != active.last_progress_pct:
                active.last_progress_pct = pct
                active.last_progress_emit_at = now
                active.last_progress_bytes = downloaded
                return True
            return False

        if active.last_progress_emit_at <= 0:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        if downloaded - active.last_progress_bytes >= PREFETCH_PROGRESS_MIN_BYTES:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        if now - active.last_progress_emit_at >= PREFETCH_PROGRESS_MIN_INTERVAL_SECONDS:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        return False
