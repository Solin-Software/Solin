"""
memorial.py ─ Solin
==============================
Serviço de mídia da Celebração do Memorial JW.

Responsabilidades:
  1. Expor a API pública de mídia do Memorial para a UI
  2. Determinar a data e a semana do Memorial para consultas síncronas leves
  3. Orquestrar a thread dedicada que executa I/O bloqueante
  4. Emitir sinais para a UI (main thread via QueuedConnection)

Threading:
  MemorialWorker   — QObject num QThread dedicado (toda I/O bloqueante aqui)
  MemorialService  — QObject na main thread (API pública para a UI)

Política de fetch:
  O worker só busca mídias se a data atual está a ≤ 7 dias do Memorial.
  Fora desse período, emite memorial_not_yet/memorial_past (UI pode ignorar).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from . import models as meeting_models
from .jwpub_cache import JwpubChecksumStore
from .memorial_calendar import memorial_date_for_year, monday_of
from .memorial_worker import MemorialWorker

log = logging.getLogger(__name__)
_STOPPING_MEMORIAL_SERVICES: set[object] = set()


# ── MemorialService — vive na main thread ────────────────────────────────────

class MemorialService(QObject):
    """
    API pública para a UI.  Vive na main thread.
    Expõe sinais para o widget consumir resultados do worker.

    Uso típico:
        svc = MemorialService(self)
        svc.set_lang("T")
        svc.memorial_ready.connect(self._on_memorial)
        svc.load()                           # dispara worker se estiver na janela
        monday = svc.memorial_week()         # para saber em qual semana mostrar
    """

    memorial_ready   = Signal(object)     # meeting_models.MemorialData (status="ready")
    memorial_status  = Signal(str)        # status string (p/ UI genérica)
    memorial_progress = Signal(int)       # 0-100

    _sig_load     = Signal(int)
    _sig_set_lang = Signal(str)

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
    ):
        super().__init__(parent)
        self._lang   = "T"
        self._year   = date.today().year
        self._data:  Optional[meeting_models.MemorialData] = None

        self._thread = QThread(self)
        self._worker = MemorialWorker(jwpub_cache_dir, checksum_store)
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        self._worker.memorial_done.connect(self._on_done)
        self._worker.progress.connect(self.memorial_progress)
        self._worker.error.connect(self._on_error)

        self._sig_load.connect(self._worker.load_memorial)
        self._sig_set_lang.connect(self._worker.set_lang)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread do Memorial."""
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
                _STOPPING_MEMORIAL_SERVICES.add(self)

                def release_service() -> None:
                    _STOPPING_MEMORIAL_SERVICES.discard(self)
                    self.deleteLater()

                thread.finished.connect(release_service)

    def __del__(self):
        try:
            self.shutdown(wait_ms=0)
        except Exception:  # noqa: BLE001 - destructors must never raise during shutdown
            log.debug("Failed to shutdown MemorialService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        """Define o idioma de mídia JW (não o idioma da interface)."""
        if lang == self._lang:
            return
        self._lang = lang
        self._data  = None
        self._sig_set_lang.emit(lang)

    def get_lang(self) -> str:
        return self._lang

    def load(self, force: bool = False):
        """
        Dispara o worker para carregar/verificar as mídias do Memorial.
        Se force=False e já temos dados prontos, emite o sinal imediatamente.
        """
        if not force and self._data and self._data.status == "ready":
            self.memorial_ready.emit(self._data)
            return
        self._sig_load.emit(self._year)

    def get_data(self) -> Optional[meeting_models.MemorialData]:
        return self._data

    def memorial_date(self) -> Optional[date]:
        """Data calculada do Memorial (pode ser None se ainda não calculada)."""
        if self._data:
            return self._data.memorial_date
        # Cálculo síncrono leve para uso imediato pela UI (sem bloquear)
        return memorial_date_for_year(self._year)

    def memorial_week(self) -> Optional[date]:
        """Segunda-feira da semana do Memorial."""
        d = self.memorial_date()
        return monday_of(d) if d else None

    def is_memorial_week(self, monday: date) -> bool:
        """Verdadeiro se a semana dada é a semana do Memorial."""
        mw = self.memorial_week()
        return mw is not None and mw == monday

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot(object)
    def _on_done(self, data: meeting_models.MemorialData):
        self._data = data
        self.memorial_status.emit(data.status)
        if data.status == "ready":
            self.memorial_ready.emit(data)

    @Slot(str)
    def _on_error(self, msg: str):
        log.error("MemorialService: %s", msg)
