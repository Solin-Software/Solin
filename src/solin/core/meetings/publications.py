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
  • resolução síncrona de mídia durante construção da UI
"""

from __future__ import annotations

import logging
import os
import itertools
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QThread,
    Signal,
    Slot,
)

from solin.core.jw.publication_archive import resolve_meeting_media
from . import models as meeting_models
from .jwpub_cache import JwpubChecksumStore
from .publication_worker import JwpubWorker

log = logging.getLogger(__name__)
_STOPPING_PUBLICATION_SERVICES: set[object] = set()


@dataclass(slots=True)
class _WeekLoadRequest:
    monday: date
    force: bool
    language: str
    is_sign_language: bool
    generation: int
    priority: int
    materialize_cached_publications: frozenset[str]
    known_wt_issue: str
    persisted_source_checksums: dict[str, str]
    order: int
    repair_cache_paths: tuple[str, ...] = ()


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
      media_resolved(request_id, metadata)
    """

    mwb_ready = Signal(str, object)
    wt_ready = Signal(str, object)
    cbs_ready = Signal(str, object)
    week_ready = Signal(str, object)
    progress = Signal(str, str, int)
    error_sig = Signal(str, str, str)
    context_progress = Signal(str, str, int, str, bool, int)
    context_error = Signal(str, str, str, str, bool, int)
    context_load_finished = Signal(str, str, bool, int)
    media_resolved = Signal(str, object)  # request_id, resolved metadata

    # Sinais internos para o worker (despacham para a worker thread)
    _sig_load_week = Signal(object, bool, str, bool, int, object, str, object, object)
    _sig_set_lang = Signal(str)
    _sig_set_sign_language = Signal(bool)
    _sig_resolve = Signal(str, str, int, int, int, str, str, bool)

    def __init__(
        self,
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._active: dict[tuple[str, str, bool, int], meeting_models.WeekData] = {}
        self._lang = "T"
        self._is_sign_language = False
        self._load_order = itertools.count()
        self._pending_loads: dict[
            tuple[str, str, bool, int],
            _WeekLoadRequest,
        ] = {}
        self._active_load_key: tuple[str, str, bool, int] | None = None

        # Cria worker + thread dedicada
        self._thread = QThread(self)
        self._worker = JwpubWorker(
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
        self._worker.load_finished.connect(self._on_load_finished)
        self._worker.media_resolved.connect(self.media_resolved)

        # JwpubService → Worker (worker thread, QueuedConnection automática)
        self._sig_load_week.connect(self._worker.load_week)
        self._sig_set_lang.connect(self._worker.set_lang)
        self._sig_set_sign_language.connect(self._worker.set_sign_language)
        self._sig_resolve.connect(self._worker.resolve_media_async)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread de reuniões."""
        self._pending_loads.clear()
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.request_cancel()
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
                _STOPPING_PUBLICATION_SERVICES.add(self)

                def release_service() -> None:
                    _STOPPING_PUBLICATION_SERVICES.discard(self)
                    self.deleteLater()

                thread.finished.connect(release_service)

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

    def load_week(
        self,
        monday: date,
        force: bool = False,
        *,
        language_code: str | None = None,
        is_sign_language: bool | None = None,
        generation: int = 0,
        priority: int = 1,
        materialize_cached_publications: Collection[str] | None = None,
        known_wt_issue: str = "",
        persisted_source_checksums: Mapping[str, str] | None = None,
        repair_cache_paths: Collection[str] | None = None,
    ):
        language = (language_code or self._lang).strip() or "T"
        is_sign = self._is_sign_language if is_sign_language is None else bool(is_sign_language)
        key = self._active_key(monday, language, is_sign, generation)
        pending = self._pending_loads.get(key)
        if pending is not None:
            pending.priority = max(pending.priority, int(priority))
            pending.materialize_cached_publications |= frozenset(
                str(pub_type)
                for pub_type in (materialize_cached_publications or ())
                if pub_type in {"mwb", "wt"}
            )
            pending.repair_cache_paths = tuple(
                sorted(
                    {
                        *pending.repair_cache_paths,
                        *(
                            str(path)
                            for path in (repair_cache_paths or ())
                            if isinstance(path, (str, os.PathLike)) and str(path)
                        ),
                    }
                )
            )
            return
        if self._active_load_key == key:
            return
        if not force and key in self._active:
            # Já existe entrada — só recarrega se alguma metade da semana falhou.
            existing = self._active[key]
            if existing.mwb_status not in ("error",) and existing.wt_status not in ("error",):
                return
        wd = meeting_models.WeekData(
            monday=monday,
            language_code=language,
            is_sign_language=is_sign,
            request_generation=max(0, int(generation)),
        )
        self._active[key] = wd
        self._pending_loads[key] = _WeekLoadRequest(
            monday,
            force,
            language,
            is_sign,
            max(0, int(generation)),
            int(priority),
            frozenset(
                {"mwb", "wt"}
                if materialize_cached_publications is None
                else {
                    str(pub_type)
                    for pub_type in materialize_cached_publications
                    if pub_type in {"mwb", "wt"}
                }
            ),
            str(known_wt_issue or ""),
            {
                str(pub_type): str(checksum or "")
                for pub_type, checksum in (persisted_source_checksums or {}).items()
                if pub_type in {"mwb", "wt"}
            },
            next(self._load_order),
            tuple(
                sorted(
                    str(path)
                    for path in (repair_cache_paths or ())
                    if isinstance(path, (str, os.PathLike)) and str(path)
                )
            ),
        )
        self._dispatch_week_load()

    def promote_week(
        self,
        monday: date,
        *,
        language_code: str,
        is_sign_language: bool,
        generation: int,
        priority: int,
    ) -> None:
        key = self._active_key(
            monday,
            language_code,
            is_sign_language,
            generation,
        )
        pending = self._pending_loads.get(key)
        if pending is not None:
            pending.priority = max(pending.priority, int(priority))

    def cancel_pending_week(
        self,
        monday: date,
        *,
        language_code: str,
        is_sign_language: bool,
        generation: int,
    ) -> None:
        key = self._active_key(
            monday,
            language_code,
            is_sign_language,
            generation,
        )
        self._pending_loads.pop(key, None)

    def _dispatch_week_load(self) -> None:
        if self._active_load_key is not None or not self._pending_loads:
            return
        key, request = max(
            self._pending_loads.items(),
            key=lambda item: (item[1].priority, -item[1].order),
        )
        self._pending_loads.pop(key, None)
        self._active_load_key = key
        self._sig_load_week.emit(
            request.monday,
            request.force,
            request.language,
            request.is_sign_language,
            request.generation,
            request.materialize_cached_publications,
            request.known_wt_issue,
            request.persisted_source_checksums,
            request.repair_cache_paths,
        )

    def get_week_data(
        self,
        monday: date,
        *,
        language_code: str | None = None,
        is_sign_language: bool | None = None,
    ) -> Optional[meeting_models.WeekData]:
        language = (language_code or self._lang).strip() or "T"
        is_sign = self._is_sign_language if is_sign_language is None else bool(is_sign_language)
        candidates = [
            week
            for active_key, week in self._active.items()
            if active_key[:3] == (monday.isoformat(), language, is_sign)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda week: week.request_generation)

    def resolve_media_async(self, request_id: str, item: "meeting_models.MeetingMedia"):
        """Resolve meeting media metadata without blocking the main thread."""
        self._sig_resolve.emit(
            request_id,
            item.key_symbol,
            item.track,
            item.issue_tag,
            item.meps_doc_id,
            item.media_type,
            self._lang,
            self._is_sign_language,
        )

    def resolve_media(self, item: "meeting_models.MeetingMedia") -> dict:
        """Resolve meeting media synchronously for explicit user actions."""
        return resolve_meeting_media(
            item.key_symbol,
            item.track,
            item.issue_tag,
            item.meps_doc_id,
            self._lang,
            is_sign_language=self._is_sign_language,
            media_type=item.media_type,
        )

    def clear_week(self, monday: date):
        monday_key = monday.isoformat()
        for key in [key for key in self._active if key[0] == monday_key]:
            self._active.pop(key, None)

    def clear_all(self) -> None:
        self._active.clear()

    @staticmethod
    def _active_key(
        monday: date,
        language: str,
        is_sign_language: bool,
        generation: int = 0,
    ) -> tuple[str, str, bool, int]:
        return (
            monday.isoformat(),
            language,
            bool(is_sign_language),
            max(0, int(generation)),
        )

    @classmethod
    def _active_key_for_week_data(
        cls,
        wd: meeting_models.WeekData,
    ) -> tuple[str, str, bool, int]:
        return cls._active_key(
            wd.monday,
            wd.language_code or "T",
            wd.is_sign_language,
            wd.request_generation,
        )

    # ── Worker callbacks (chegam na main thread via QueuedConnection) ─────────

    @Slot(str, object)
    def _on_mwb_done(self, key: str, wd: meeting_models.WeekData):
        active_key = self._active_key_for_week_data(wd)
        existing = self._active.get(active_key)
        if existing:
            existing.mwb_pub_dir = wd.mwb_pub_dir
            existing.mwb_cover_bytes = wd.mwb_cover_bytes
            existing.mwb_date_label = wd.mwb_date_label
            existing.mwb_week_title = wd.mwb_week_title
            existing.mwb_all_media = wd.mwb_all_media
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.mwb_status = wd.mwb_status
            existing.mwb_issue = wd.mwb_issue
            existing.mwb_source_checksum = wd.mwb_source_checksum
            existing.cbs_ref = wd.cbs_ref
            existing.cbs_status = wd.cbs_status
            self.mwb_ready.emit(key, existing)
            self._check_complete(key, existing)
        else:
            self._active[active_key] = wd
            self.mwb_ready.emit(key, wd)
            self._check_complete(key, wd)

    @Slot(str, object)
    def _on_wt_done(self, key: str, wd: meeting_models.WeekData):
        active_key = self._active_key_for_week_data(wd)
        existing = self._active.get(active_key)
        if existing:
            existing.wt_pub_dir = wd.wt_pub_dir
            existing.wt_cover_bytes = wd.wt_cover_bytes
            existing.wt_study_title = wd.wt_study_title
            existing.wt_issue = wd.wt_issue
            existing.wt_source_checksum = wd.wt_source_checksum
            existing.wt_all_media = wd.wt_all_media
            existing.wt_status = wd.wt_status
            self.wt_ready.emit(key, existing)
            self._check_complete(key, existing)
        else:
            self._active[active_key] = wd
            self.wt_ready.emit(key, wd)
            self._check_complete(key, wd)

    @Slot(str, object)
    def _on_cbs_done(self, key: str, wd: meeting_models.WeekData):
        active_key = self._active_key_for_week_data(wd)
        existing = self._active.get(active_key)
        if existing:
            existing.cbs_pub_dir = wd.cbs_pub_dir
            existing.cbs_items = wd.cbs_items
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.cbs_status = wd.cbs_status
            self.cbs_ready.emit(key, existing)
        else:
            self._active[active_key] = wd
            self.cbs_ready.emit(key, wd)

    @Slot(str, str, int, str, bool, int)
    def _on_worker_progress(
        self,
        key: str,
        pub: str,
        pct: int,
        language: str,
        is_sign_language: bool,
        generation: int,
    ):
        self.progress.emit(key, pub, pct)
        self.context_progress.emit(
            key,
            pub,
            pct,
            language,
            is_sign_language,
            generation,
        )

    @Slot(str, str, str, str, bool, int)
    def _on_worker_error(
        self,
        key: str,
        pub: str,
        msg: str,
        language: str,
        is_sign_language: bool,
        generation: int,
    ):
        # "NOT_FOUND" is a sentinel emitted by the worker when the JW API
        # confirmed the publication does not exist (empty files list).
        # Any other message means a generic connectivity / extraction failure.
        status = "not_found" if msg == "NOT_FOUND" else "error"
        wd = self._active.get(
            (key, language, bool(is_sign_language), max(0, int(generation))),
        )
        if wd:
            if pub == "mwb":
                wd.mwb_status = status
            elif pub == "wt":
                wd.wt_status = status
        self.error_sig.emit(key, pub, msg)
        self.context_error.emit(
            key,
            pub,
            msg,
            language,
            is_sign_language,
            generation,
        )

    @Slot(str, str, bool, int)
    def _on_load_finished(
        self,
        monday_text: str,
        language: str,
        is_sign_language: bool,
        generation: int,
    ) -> None:
        key = (
            monday_text,
            language,
            bool(is_sign_language),
            max(0, int(generation)),
        )
        if self._active_load_key == key:
            self._active_load_key = None
        self.context_load_finished.emit(
            monday_text,
            language,
            is_sign_language,
            generation,
        )
        self._dispatch_week_load()

    def _check_complete(self, key: str, wd: meeting_models.WeekData):
        if wd and wd.mwb_status in ("ready", "empty") and wd.wt_status in ("ready", "empty"):
            self.week_ready.emit(key, wd)


# ── Public helpers ────────────────────────────────────────────────────────────
