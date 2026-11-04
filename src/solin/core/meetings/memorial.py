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
import time
from dataclasses import dataclass, fields
from datetime import date
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from solin.core.media.publication_assets import PublicationAssetStore

from PySide6.QtCore import QObject, QThread, Signal, Slot

from . import models as meeting_models
from .jwpub_cache import JwpubChecksumStore
from .memorial_calendar import memorial_date_for_year, monday_of
from .memorial_worker import MemorialWorker
from .content_policy import MeetingContentError, MeetingContentOperation, MeetingContentPolicy
from .tree_builder import MeetingTreeBuilder
from .tree_store import MeetingTreeStore, MeetingTreeOverview
from .tree_types import iter_nodes

log = logging.getLogger(__name__)
_STOPPING_MEMORIAL_SERVICES: set[object] = set()


@dataclass(frozen=True)
class _MemorialRequest:
    language: str
    year: int
    context_generation: int
    operation: MeetingContentOperation


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

    memorial_ready = Signal(object)  # meeting_models.MemorialData (status="ready")
    memorial_status = Signal(str)  # status string for generic UI use
    memorial_progress = Signal(int)  # 0-100

    state_changed = Signal()
    content_settled = Signal()
    asset_changed = Signal(str)
    _sig_load = Signal(int, object)
    _sig_set_lang = Signal(str)

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
        *,
        asset_store: PublicationAssetStore | None = None,
        content_policy: MeetingContentPolicy | None = None,
        protected_publication_sources: Callable[[], Iterable[str]] | None = None,
        tree_store: MeetingTreeStore | None = None,
    ):
        super().__init__(parent)
        self.content_policy = content_policy or MeetingContentPolicy()
        self.asset_store = asset_store
        self._tree_store = tree_store
        self._loading = False
        self._stopped = False
        self._context_generation = 0
        self._active_request: _MemorialRequest | None = None
        self._last_checked: dict[str, float] = {}
        self._observed_checksums: dict[str, str] = {}
        self._pending_request: _MemorialRequest | None = None
        self._repair_update_required = False
        self._lang = "T"
        self._year = date.today().year
        self._data: Optional[meeting_models.MemorialData] = None

        self._thread = QThread(self)
        self._worker = MemorialWorker(
            jwpub_cache_dir,
            checksum_store,
            asset_store=asset_store,
            content_policy=self.content_policy,
            protected_publication_sources=protected_publication_sources,
        )
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        self._worker.content_checked.connect(self._on_content_checked)
        self._worker.finished.connect(self._on_finished)
        self._worker.saved_ready.connect(self._on_saved_ready)
        self._worker.asset_changed.connect(self.asset_changed)
        self._worker.memorial_done.connect(self._on_done)
        self._worker.progress.connect(self.memorial_progress)
        self._worker.error.connect(self._on_error)

        self._sig_load.connect(self._worker.load_memorial)
        self._sig_set_lang.connect(self._worker.set_lang)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Explicitly stop the Memorial worker thread."""
        self._stopped = True
        self._pending_request = None
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
        self._context_generation += 1
        self._pending_request = None
        self._repair_update_required = False
        self._data = None
        self._sig_set_lang.emit(lang)

    def get_lang(self) -> str:
        return self._lang

    def _saved_snapshot(self):
        memorial_date = self.memorial_date()
        if self._tree_store is None or memorial_date is None:
            return None
        return self._tree_store.find_snapshot("memorial", memorial_date, self._lang)

    def has_prepared_content(self) -> bool:
        return self._saved_snapshot() is not None

    def load_saved(self) -> Optional[meeting_models.MemorialData]:
        """Restore the durable official baseline without HTTP or archive access."""
        snapshot = self._saved_snapshot()
        if snapshot is None:
            return None
        allowed = {value.name for value in fields(meeting_models.MeetingMedia)}
        media = []
        for node in iter_nodes(snapshot.canonical_nodes or snapshot.nodes):
            ref = node.get("media_ref")
            if node.get("type") == "media" and isinstance(ref, dict):
                media.append(
                    meeting_models.MeetingMedia(
                        **{key: value for key, value in ref.items() if key in allowed}
                    )
                )
        self._data = meeting_models.MemorialData(
            year=self._year,
            memorial_date=snapshot.monday,
            memorial_week=monday_of(snapshot.monday),
            cover_bytes=snapshot.overview.cover_bytes,
            videos=media,
            status="ready",
            language_code=self._lang,
            source_checksum=snapshot.source_checksum,
            publication_dependencies=snapshot.publication_dependencies,
        )
        self.memorial_ready.emit(self._data)
        return self._data

    @property
    def update_available(self) -> bool:
        snapshot = self._saved_snapshot()
        observed = self._observed_checksums.get(self._lang, "")
        if not observed and snapshot is not None:
            observed = snapshot.observed_publication_checksums.get(
                f"mi{str(self._year)[2:]}:{self._lang}:0", ""
            )
        return bool(
            snapshot
            and snapshot.source_checksum
            and observed
            and snapshot.source_checksum != observed
        )

    @property
    def repair_required(self) -> bool:
        snapshot = self._saved_snapshot()
        if snapshot is None:
            return False
        for node in iter_nodes(snapshot.canonical_nodes or snapshot.nodes):
            ref = node.get("media_ref") or {}
            if str(ref.get("mime_type") or "").startswith("image/"):
                path = str(ref.get("file_path") or "")
                if path and not Path(path).is_file():
                    return True
        return False

    @property
    def repair_requires_update(self) -> bool:
        return self._repair_update_required and self.repair_required

    def _request(self, operation: MeetingContentOperation) -> _MemorialRequest:
        return _MemorialRequest(self._lang, self._year, self._context_generation, operation)

    def _is_current(self, request: _MemorialRequest) -> bool:
        return (
            not self._stopped
            and request.language == self._lang
            and request.year == self._year
            and request.context_generation == self._context_generation
        )

    def _accepts_worker_result(self) -> bool:
        return not self._stopped and (
            self._active_request is None or self._is_current(self._active_request)
        )

    def _start_operation(
        self,
        operation: MeetingContentOperation,
        saved: meeting_models.MemorialData | None,
    ) -> None:
        self._active_request = self._request(operation)
        self._loading = True
        self._sig_load.emit(
            self._year,
            {
                "force": operation == MeetingContentOperation.UPDATE,
                "saved": saved,
                "operation": operation,
            },
        )

    def repair_saved(self) -> None:
        if self._stopped:
            return
        if self._loading:
            self._pending_request = self._request(MeetingContentOperation.REPAIR)
            return
        saved = self._data or self.load_saved()
        if saved is not None:
            self._start_operation(MeetingContentOperation.REPAIR, saved)

    def load(self, force: bool = False):
        if self._stopped:
            return
        if self._loading:
            if force:
                self._pending_request = self._request(MeetingContentOperation.UPDATE)
            return
        saved = self._data or self.load_saved()
        if (
            not force
            and saved is not None
            and (
                not self.content_policy.auto_recover_images
                or all(
                    item.media_type != "image" or Path(item.file_path).is_file()
                    for item in saved.videos
                )
            )
            and time.monotonic() - self._last_checked.get(self._lang, float("-inf")) < 6 * 60 * 60
        ):
            self.memorial_ready.emit(saved)
            return
        operation = (
            MeetingContentOperation.UPDATE
            if force
            else MeetingContentOperation.CHECK
            if saved is not None
            else MeetingContentOperation.PREPARE
        )
        self._start_operation(operation, saved)

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

    @Slot(str, int, str)
    def _on_content_checked(self, language: str, year: int, checksum: str):
        if (
            not self._accepts_worker_result()
            or language != self._lang
            or year != self._year
            or not checksum
        ):
            return
        self._observed_checksums[language] = checksum
        self._last_checked[language] = time.monotonic()
        snapshot = self._saved_snapshot()
        if snapshot is not None and self._tree_store is not None:
            try:
                self._tree_store.observe_publications(
                    snapshot.tree_key, {f"mi{str(year)[2:]}:{language}:0": checksum}
                )
            except (OSError, UnicodeError, ValueError) as exc:
                self._on_error(str(exc))
        self.state_changed.emit()

    @Slot()
    def _on_finished(self):
        self._loading = False
        self._active_request = None
        self._worker.release_assets()
        self.content_settled.emit()
        self.state_changed.emit()
        pending, self._pending_request = self._pending_request, None
        if pending is None or not self._is_current(pending):
            return
        if pending.operation == MeetingContentOperation.UPDATE:
            self.load(force=True)
        elif pending.operation == MeetingContentOperation.REPAIR:
            self.repair_saved()

    @Slot(object)
    def _on_saved_ready(self, data: meeting_models.MemorialData):
        if not self._accepts_worker_result() or (
            data.language_code and data.language_code != self._lang
        ):
            return
        self._data = data
        self.state_changed.emit()
        self.memorial_ready.emit(data)

    @Slot(object)
    def _on_done(self, data: meeting_models.MemorialData):
        if not self._accepts_worker_result() or (
            data.language_code and data.language_code != self._lang
        ):
            return
        if (
            data.status == "ready"
            and self._tree_store is not None
            and data.memorial_date is not None
        ):
            builder = MeetingTreeBuilder()
            canonical = builder.build_memorial(data)
            tree_key = f"memorial:{data.memorial_date.isoformat()}:{self._lang}:{data.year}"
            try:
                self._tree_store.reconcile(
                    tree_key,
                    canonical,
                    builder.canonical_hash(canonical),
                    MeetingTreeOverview(cover_bytes=data.cover_bytes),
                    fallback=self._saved_snapshot(),
                    source_checksum=data.source_checksum,
                    publication_dependencies=data.publication_dependencies,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                self._on_error(str(exc))
                return
        if data.status == "ready":
            self._repair_update_required = False
        if data.status in {"ready", "empty"} or self._data is None:
            self._data = data
        self.state_changed.emit()
        self.memorial_status.emit(data.status)
        if data.status == "ready":
            self.memorial_ready.emit(data)

    @Slot(str)
    def _on_error(self, msg: str):
        if not self._accepts_worker_result():
            return
        if msg in {
            MeetingContentError.IMAGE_REVISION_UNAVAILABLE,
            MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
        }:
            self._repair_update_required = True
            if (
                self.content_policy.auto_update
                and self._active_request is not None
                and self._active_request.operation != MeetingContentOperation.UPDATE
            ):
                self._pending_request = self._request(MeetingContentOperation.UPDATE)
            self.state_changed.emit()
        log.error("MemorialService: %s", msg)
