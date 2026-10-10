"""
memorial.py — Solin
===================
JW Memorial media service.

Responsibilities:
  1. Expose the public Memorial media API to the UI.
  2. Determine the Memorial date and week for lightweight synchronous queries.
  3. Orchestrate the dedicated thread performing blocking I/O.
  4. Emit UI signals (main thread via QueuedConnection).

Threading:
  MemorialWorker — QObject in a dedicated QThread (all blocking I/O).
  MemorialService — QObject on the main thread (public UI API).

Fetch policy:
  The worker fetches media only when the current date is ≤ 7 days before
  the Memorial. Outside that window, it emits memorial_not_yet/memorial_past
  (the UI may ignore these).
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


# MemorialService: runs on the main thread.

class MemorialService(QObject):
    """
    Public UI API, running on the main thread.
    Expose signals so the widget can consume worker results.

    Typical usage:
        svc = MemorialService(self)
        svc.set_lang("T")
        svc.memorial_ready.connect(self._on_memorial)
        svc.load()  # start the worker if within the window
        monday = svc.memorial_week()  # determine which week to display
    """

    memorial_ready   = Signal(object)     # meeting_models.MemorialData (status="ready")
    memorial_status  = Signal(str)        # status string for generic UI use
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
        """Explicitly stop the Memorial worker thread."""
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
        """Set the JW media language (not the interface language)."""
        if lang == self._lang:
            return
        self._lang = lang
        self._data  = None
        self._sig_set_lang.emit(lang)

    def get_lang(self) -> str:
        return self._lang

    def load(self, force: bool = False):
        """
        Start the worker to load/check Memorial media.
        If force=False and data is already ready, emit the signal immediately.
        """
        if not force and self._data and self._data.status == "ready":
            self.memorial_ready.emit(self._data)
            return
        self._sig_load.emit(self._year)

    def get_data(self) -> Optional[meeting_models.MemorialData]:
        return self._data

    def memorial_date(self) -> Optional[date]:
        """Calculated Memorial date (may be None if not yet calculated)."""
        if self._data:
            return self._data.memorial_date
        # Lightweight synchronous calculation for immediate, nonblocking UI use.
        return memorial_date_for_year(self._year)

    def memorial_week(self) -> Optional[date]:
        """Monday of the Memorial week."""
        d = self.memorial_date()
        return monday_of(d) if d else None

    def is_memorial_week(self, monday: date) -> bool:
        """True if the given week is the Memorial week."""
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
