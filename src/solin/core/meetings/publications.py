"""
publications.py ─ Solin
==========================
Main-thread Qt service facade for meeting JWPUB publications.

Threading model (production-grade):
────────────────────────────────────
REGRA ABSOLUTA: nada que bloqueie (HTTP, disk I/O pesado, SQLite) roda na
main thread. Toda comunicação entre worker e UI é via Signal/Slot com
QueuedConnection automática (QThread garante a thread affinity correta).

JwpubWorker    — QObject que vive num QThread dedicado.
                 Faz toda a lógica: fetch de URL, download, extração zip,
                 parse SQLite. Emite sinais de resultado para a main thread.
JwpubService   — QObject na main thread. Cria/gerencia o worker thread,
                 recebe sinais e repassa para a UI.

Isso elimina:
  • HTTP bloqueante na main thread (_get_jwpub_url era síncrono)
  • QRunnable + _Signals com moveToThread (AutoConnection frágil)
  • threading.Thread emitindo signals (sem QThread wrapper = DirectConnection)
  • resolve_video() síncrono chamado de _MediaRow no __init__
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import (
    QObject, QThread, Signal, Slot,
)

from solin.core.media.cache import MediaCacheManager
from solin.core.media.settings import MediaSettingsStore
from . import models as meeting_models
from .jwpub_cache import JwpubChecksumStore
from .meeting_weeks import (
    current_monday,
)
from .publication_worker import (
    JwpubWorker,
    resolve_meeting_video,
)

log = logging.getLogger(__name__)

# ── JwpubService — vive na main thread, gerencia o worker thread ──────────────

class JwpubService(QObject):
    """
    API pública para a UI. Vive na main thread.
    Cria um JwpubWorker num QThread dedicado e encaminha pedidos via sinais.

    Sinais para a UI:
      mwb_ready(key, WeekData)
      wt_ready(key, WeekData)
      cbs_ready(key, WeekData)
      week_ready(key, WeekData)
      progress(key, pub, pct)
      error_sig(key, pub, msg)
      video_resolved(request_id, url, title, thumbnail)
    """
    mwb_ready      = Signal(str, object)
    wt_ready       = Signal(str, object)
    cbs_ready      = Signal(str, object)
    week_ready     = Signal(str, object)
    progress       = Signal(str, str, int)
    error_sig      = Signal(str, str, str)
    video_resolved = Signal(str, str, str, str)   # request_id, url, title, thumb

    # Sinais internos para o worker (despacham para a worker thread)
    _sig_load_week         = Signal(object, bool)
    _sig_set_lang          = Signal(str)
    _sig_set_sign_language = Signal(bool)
    _sig_resolve           = Signal(str, str, int, int, int, str)
    _sig_prefetch_wd       = Signal(object)

    def __init__(
        self,
        media_settings: MediaSettingsStore,
        cache_manager: MediaCacheManager,
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_settings = media_settings
        self._cache_manager = cache_manager
        self._active: dict[str, meeting_models.WeekData] = {}
        self._lang   = "T"
        self._is_sign_language = False

        # Cria worker + thread dedicada
        self._thread = QThread(self)
        self._worker = JwpubWorker(
            cache_manager.media_cache_dir,
            jwpub_cache_dir,
            checksum_store,
        )
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        # Worker → JwpubService (main thread, QueuedConnection automática)
        self._worker.mwb_done.connect(self._on_mwb_done)
        self._worker.wt_done.connect(self._on_wt_done)
        self._worker.cbs_done.connect(self._on_cbs_done)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.error.connect(self._on_worker_error)
        self._worker.video_resolved.connect(self.video_resolved)
        self._worker.prefetch_requested.connect(self._on_prefetch_requested)

        # JwpubService → Worker (worker thread, QueuedConnection automática)
        self._sig_load_week.connect(self._worker.load_week)
        self._sig_set_lang.connect(self._worker.set_lang)
        self._sig_set_sign_language.connect(self._worker.set_sign_language)
        self._sig_resolve.connect(self._worker.resolve_video_async)
        self._sig_prefetch_wd.connect(self._worker.prefetch_week_media)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread de reuniões."""
        thread: QThread | None = getattr(self, "_thread", None)
        if thread is None:
            return
        try:
            running = thread.isRunning()
        except RuntimeError:
            self._thread = None
            return

        if running:
            thread.quit()
            thread.wait(wait_ms)
            try:
                still_running = thread.isRunning()
            except RuntimeError:
                self._thread = None
                return
            if still_running and delete_when_stopped:
                self.setParent(None)
                thread.finished.connect(self.deleteLater)

    def __del__(self):
        try:
            self.shutdown(wait_ms=0)
        except Exception:  # noqa: BLE001 - destructors must never raise during interpreter shutdown
            log.debug("Failed to shutdown JwpubService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        self._lang = lang
        self._sig_set_lang.emit(lang)

    def set_sign_language(self, is_sign: bool) -> None:
        """
        Informa ao worker se o idioma de mídia é gestual.
        Quando True, cânticos com key_symbol='sjjm' são resolvidos como 'sjj'.
        Deve ser chamado sempre que o idioma de mídia mudar.
        """
        self._is_sign_language = bool(is_sign)
        self._sig_set_sign_language.emit(is_sign)

    def get_lang(self) -> str:
        return self._lang

    def is_sign_language(self) -> bool:
        return self._is_sign_language

    def load_week(self, monday: date, force: bool = False):
        key = monday.isoformat()
        if not force and key in self._active:
            # Já existe entrada — só recarrega se alguma metade da semana falhou.
            existing = self._active[key]
            if existing.mwb_status not in ("error",) and existing.wt_status not in ("error",):
                return
        wd = meeting_models.WeekData(monday=monday)
        self._active[key] = wd
        self._sig_load_week.emit(monday, force)

    def get_week_data(self, monday: date) -> Optional[meeting_models.WeekData]:
        return self._active.get(monday.isoformat())

    def resolve_video_async(self, request_id: str, item: "meeting_models.MeetingMedia"):
        """
        Resolve URL de vídeo de forma assíncrona.
        Resultado chega via signal video_resolved(request_id, url, title, thumb).
        NUNCA bloqueia a main thread.
        """
        self._sig_resolve.emit(
            request_id,
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
        )

    def resolve_video(self, item: "meeting_models.MeetingMedia") -> dict:
        """
        Resolve URL de vídeo de forma SÍNCRONA (bloqueia a main thread ~200ms).
        Use apenas para ações pontuais do usuário (ex: clique em reproduzir),
        nunca durante construção de widgets ou loops.
        Para uso não-bloqueante, prefira resolve_video_async().
        """
        return resolve_meeting_video(
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
            is_sign_language=self._is_sign_language,
        )

    def clear_week(self, monday: date):
        self._active.pop(monday.isoformat(), None)

    def auto_download_if_enabled(self):
        if not self._media_settings.meetings_auto_download():
            return
        mon      = current_monday()
        next_mon = mon + timedelta(weeks=1)

        # Conecta antes de disparar load_week — garante que não perdemos o sinal
        # caso o load seja muito rápido (cache quente).
        if not getattr(self, "_auto_dl_connected", False):
            self._auto_dl_connected = True
            self.mwb_ready.connect(self._on_auto_dl_ready)
            self.wt_ready.connect(self._on_auto_dl_ready)
            self.cbs_ready.connect(self._on_auto_dl_ready)

        # Se a semana atual já está pronta (cache quente), o mwb_ready já foi emitido
        # antes desta conexão — dispara o prefetch diretamente.
        mon_wd = self._active.get(mon.isoformat())
        if mon_wd and mon_wd.mwb_status == "ready":
            self._sig_prefetch_wd.emit(mon_wd)

        # A semana atual já foi carregada por _navigate_to — não recarregar.
        # Apenas carrega a semana seguinte (que ainda não foi pedida).
        self.load_week(next_mon)

    @Slot(str, object)
    def _on_auto_dl_ready(self, key: str, wd: object):
        if not self._media_settings.meetings_auto_download():
            return
        mon = current_monday()
        target_keys = {mon.isoformat(), (mon + timedelta(weeks=1)).isoformat()}
        if key in target_keys:
            self._sig_prefetch_wd.emit(wd)

    # ── Worker callbacks (chegam na main thread via QueuedConnection) ─────────

    @Slot(str, object)
    def _on_mwb_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.mwb_pub_dir     = wd.mwb_pub_dir
            existing.mwb_cover_bytes = wd.mwb_cover_bytes
            existing.mwb_date_label  = wd.mwb_date_label
            existing.mwb_week_title  = wd.mwb_week_title
            existing.mwb_all_media   = wd.mwb_all_media
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.mwb_status      = wd.mwb_status
            existing.mwb_issue       = wd.mwb_issue
            existing.cbs_ref         = wd.cbs_ref
            existing.cbs_status      = wd.cbs_status
            self.mwb_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.mwb_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_wt_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.wt_pub_dir     = wd.wt_pub_dir
            existing.wt_cover_bytes = wd.wt_cover_bytes
            existing.wt_study_title = wd.wt_study_title
            existing.wt_issue       = wd.wt_issue
            existing.wt_all_media   = wd.wt_all_media
            existing.wt_status      = wd.wt_status
            self.wt_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.wt_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_cbs_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.cbs_pub_dir = wd.cbs_pub_dir
            existing.cbs_items   = wd.cbs_items
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.cbs_status  = wd.cbs_status
            self.cbs_ready.emit(key, existing)
        else:
            self._active[key] = wd
            self.cbs_ready.emit(key, wd)

    @Slot(str, str, int)
    def _on_worker_progress(self, key: str, pub: str, pct: int):
        self.progress.emit(key, pub, pct)

    @Slot(str, str, str)
    def _on_worker_error(self, key: str, pub: str, msg: str):
        # "NOT_FOUND" is a sentinel emitted by the worker when the JW API
        # confirmed the publication does not exist (empty files list).
        # Any other message means a generic connectivity / extraction failure.
        status = "not_found" if msg == "NOT_FOUND" else "error"
        wd = self._active.get(key)
        if wd:
            if pub == "mwb":
                wd.mwb_status = status
            elif pub == "wt":
                wd.wt_status = status
        self.error_sig.emit(key, pub, msg)

    @Slot(str)
    def _on_prefetch_requested(self, url: str):
        """Recebe pedido de prefetch do worker — já estamos na main thread."""
        mgr = self._cache_manager
        if not mgr.is_cached(url) and not mgr.is_prefetching(url):
            mgr.prefetch(url)

    def _check_complete(self, key: str):
        wd = self._active.get(key)
        if wd and wd.mwb_status in ("ready", "empty") \
                and wd.wt_status in ("ready", "empty"):
            self.week_ready.emit(key, wd)


# ── Public helpers ────────────────────────────────────────────────────────────

