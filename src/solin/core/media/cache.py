"""
MediaCacheManager — gerencia cache e pré-download de mídia remota.

O manager centraliza a fila global de prefetch para que ações em lote não
criem dezenas de SongDownloader simultâneos. UIs consultam estado ativo/queued
separadamente: ativo mostra progresso; queued mostra espera sem spinner falso.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Protocol, cast
from PySide6.QtCore import QObject, Signal, Slot

from .application import (
    MediaPrefetchQueue,
    PrefetchAction,
    PrefetchPlan,
    QueuedPrefetch,
)
from .download_storage import is_remote_url, is_url_cached


class _Downloader(Protocol):
    progress: Any
    finished: Any
    error: Any

    def cancel(self) -> None: ...

    def start(self, url: str) -> None: ...


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
        self._prefetch_queue = MediaPrefetchQueue(self)
        self._downloaders: dict[str, _Downloader] = {}
        self._downloader_factory: Callable[[QObject], _Downloader] | None = None
        self._notify_cached_requested.connect(self.notify_cached)

    @property
    def max_concurrent_prefetches(self) -> int:
        return self._prefetch_queue.max_concurrent

    @max_concurrent_prefetches.setter
    def max_concurrent_prefetches(self, value: int) -> None:
        self._prefetch_queue.max_concurrent = max(0, int(value))

    # ── API pública ───────────────────────────────────────────────────────

    def is_cached(self, url: str) -> bool:
        return is_url_cached(url, self.media_cache_dir)

    @staticmethod
    def is_remote(url: str) -> bool:
        """True se a URL é HTTP/HTTPS (portanto sujeita a cacheamento)."""
        return is_remote_url(url)

    def is_prefetching(self, url: str) -> bool:
        """True se há um prefetch ativo para a URL."""
        return self._prefetch_queue.is_prefetching(url)

    def is_queued(self, url: str) -> bool:
        """True se a URL aguarda uma vaga na fila de prefetch."""
        return self._prefetch_queue.is_queued(url)

    @Slot(str)
    def prefetch(self, url: str, priority: bool = False) -> None:
        """
        Inicia download em background para pré-cachear a mídia.
        No-op se já cacheado; se a fila estiver cheia, aguarda uma vaga.
        """
        self._apply_plan(self._prefetch_queue.prefetch(url, priority=priority))

    def prefetch_many(self, urls: list[str], batch_id: str) -> int:
        """Enfileira um lote deduplicado e retorna quantas URLs entraram na fila."""
        plan = self._prefetch_queue.prefetch_many(urls, batch_id)
        self._apply_plan(plan)
        return plan.added

    def batch_counts(self, batch_id: str) -> tuple[int, int, int, int]:
        return self._prefetch_queue.batch_counts(batch_id)

    def batch_is_active(self, batch_id: str) -> bool:
        return self._prefetch_queue.batch_is_active(batch_id)

    def cancel_prefetch(self, url: str) -> None:
        """
        Cancela o prefetch ativo para a URL (se houver).
        Deve ser chamado pelo MediaController ANTES de iniciar seu download,
        para evitar gravações simultâneas no mesmo .tmp.
        """
        self._apply_plan(self._prefetch_queue.cancel_prefetch(url))

    def cancel_all(self) -> None:
        """Cancela todos os prefetches ativos (ex: ao fechar o app)."""
        self._apply_plan(self._prefetch_queue.cancel_all())

    def cancel_batch(self, batch_id: str) -> None:
        self._apply_plan(self._prefetch_queue.cancel_batch(batch_id))

    def notify_cached(self, url: str) -> None:
        """
        Chamado pelo MediaController quando SEU download termina.
        Garante que o prefetch concorrente (se houvesse) seja removido do dict
        e emite cache_changed para atualizar a UI.
        """
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.notify_cached(url))

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
        self._apply_plan(self._prefetch_queue.pump())

    def _start_entry(self, entry: QueuedPrefetch) -> None:
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
        self._downloaders[entry.url] = dl
        dl.start(entry.url)

    def _on_prefetch_progress(self, url: str, downloaded: int, total: int) -> None:
        self._apply_plan(
            self._prefetch_queue.start_progress(url, downloaded, total)
        )

    def _on_prefetch_done(self, url: str) -> None:
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.complete(url))

    def _on_prefetch_error(self, url: str, msg: str) -> None:
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.fail(url, msg))

    def _emit_batch_changed(self, batch_id: str) -> None:
        if not batch_id:
            return
        queued, active, done, failed = self.batch_counts(batch_id)
        self.prefetch_batch_changed.emit(batch_id, queued, active, done, failed)

    def _apply_plan(self, plan: PrefetchPlan) -> None:
        for action in plan.actions:
            self._apply_action(action)

    def _apply_action(self, action: PrefetchAction) -> None:
        if action.kind == "queued":
            self.prefetch_queued.emit(action.url)
        elif action.kind == "dequeued":
            self.prefetch_dequeued.emit(action.url)
        elif action.kind == "cache_changed":
            self.cache_changed.emit(action.url)
        elif action.kind == "progress":
            self.prefetch_progress.emit(action.url, action.downloaded, action.total)
        elif action.kind == "error":
            self.prefetch_error.emit(action.url, action.message)
        elif action.kind == "batch_changed":
            self._emit_batch_changed(action.batch_id)
        elif action.kind == "batch_error":
            self.prefetch_batch_error.emit(action.batch_id, action.message)
        elif action.kind == "cancel_active":
            downloader = self._downloaders.pop(action.url, None)
            if downloader is not None:
                downloader.cancel()
        elif action.kind == "start" and action.entry is not None:
            self._start_entry(action.entry)
