"""
meeting_tree_controller.py - Solin
==================================
QML controller for meeting trees.

This intentionally implements the PlaylistTreeView protocol without inheriting
from PlaylistEditModel/PlaylistEditBridge.  Meetings have their own persistence
and merge rules while sharing the same tree surface.
"""
from __future__ import annotations

import copy
import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Property, QCoreApplication, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox, QDialog

from ...core.media.cache import MediaCacheManager
from ...core.foundation import paths as _paths
from ...core.foundation.constants import (
    AUDIO_EXTS,
    DOCX_EXTS,
    IMAGE_EXTS,
    JWPUB_EXTS,
    MEDIA_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
    THUMB_JPEG_QUALITY,
)
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from ...core.ingest.manifest import ManifestError, cache_dir
from ...core.meetings.memorial import MemorialData
from ...core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync,
    MeetingSyncRecord,
    MeetingSyncError,
    MeetingSyncIdentity,
)
from ...core.meetings.publications import MeetingMedia, WeekData
from ...core.meetings.thumbnails import (
    meeting_thumb_cache_key,
    meeting_thumb_dir,
    meeting_thumb_path,
)
from ...core.meetings.tree_builder import MeetingTreeBuilder
from ...core.meetings.tree_merger import MeetingTreeMerger
from ...core.meetings.tree_store import MeetingTreeStore
from ...core.meetings.tree_types import Node, clone_nodes, count_media, iter_nodes, new_node_id
from ...core.rendering.pdf import PdfConvertThread, cached_pages as pdf_cached_pages
from ...core.rendering.libreoffice import (
    LoConvertThread,
    cached_pages as lo_cached_pages,
    libreoffice_available,
)
from ...core.meetings.colors import generate_section_hue, section_colors
from ..playlist.dialogs import _HuePickerDialog, _NameDialog
from ..media_info_extractor import MediaInfoQueue, is_filename_title
from ..playlist.edit_visuals import _format_duration

_BIG_INDEX = 2**31 - 1
_MEDIA_FIELDS = set(MeetingMedia.__dataclass_fields__.keys())


def _tr(context: str, source: str) -> str:
    return QCoreApplication.translate(context, source)


def _clean_title(value: str) -> str:
    return (value or "").strip()


def _media_type_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "video"


def _mime_for(path: str, media_type: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if media_type == "image":
        return "image/*"
    if media_type == "audio":
        return "audio/*"
    return "video/*"


def _meeting_media_from_ref(ref: dict[str, Any]) -> MeetingMedia:
    data = {key: ref.get(key) for key in _MEDIA_FIELDS if key in ref}
    return MeetingMedia(**data)


def _has_jw_media_identity(ref: dict[str, Any]) -> bool:
    return bool(str(ref.get("key_symbol") or "").strip() or ref.get("meps_doc_id"))


def _usable_ref_file_path(ref: dict[str, Any]) -> str:
    path = str(ref.get("file_path") or "")
    if not path:
        return ""
    if MediaCacheManager.is_remote(path) or os.path.exists(path):
        return path
    if _has_jw_media_identity(ref):
        return ""
    return path


def _ref_title(ref: dict[str, Any]) -> str:
    return _clean_title(str(ref.get("label") or ref.get("caption") or ""))


class MeetingTreeController(QObject):
    backRequested = Signal()
    projectRequested = Signal(object)
    pointerEntered = Signal()
    pointerExited = Signal()

    stateChanged = Signal()
    chromeChanged = Signal()
    mediaChanged = Signal(str, str, str, str)
    mediaInserted = Signal(str, int, "QVariant")
    nodesInserted = Signal(str, int, "QVariant")
    nodeReplaced = Signal(str, "QVariant")
    sectionCountsChanged = Signal("QVariant")
    markerEditRequested = Signal(str)
    cloudChanged = Signal(str, bool, bool, float, str)
    syncStateChanged = Signal()

    def __init__(
        self,
        service,
        *,
        meeting_type: str,
        language_code: str,
        fallback_language_code: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._svc = service
        self._meeting_type = meeting_type
        self._language_code = language_code or "E"
        self._fallback_language_code = fallback_language_code or self._language_code
        self._store = MeetingTreeStore()
        self._builder = MeetingTreeBuilder()
        self._sync_service = MeetingLinkedFolderSync()
        self._nodes: list[Node] = []
        self._tree_key = ""
        self._canonical_hash = ""
        self._sync_identity: MeetingSyncIdentity | None = None
        self._sync_root = ""
        self._sync_folder = ""
        self._sync_available = False
        self._sync_enabled = False
        self._sync_busy = False
        self._sync_revision = 0
        self._deleted_source_keys: set[str] = set()
        self._playlist_name = ""
        self._thumb_cache: dict[str, QPixmap] = {}
        self._thumb_versions: dict[str, int] = {}
        self._info_queue = MediaInfoQueue(self)
        self._info_queue.info_ready.connect(self._on_info_ready)
        self._info_queue.duration_ready.connect(self._on_duration_ready)
        self._token_to_node_id: dict[int, str] = {}
        self._active_info_requests: set[tuple[str, str]] = set()
        self._next_token = 1
        self._resolve_to_node_id: dict[str, str] = {}
        self._resolved_urls: dict[str, str] = {}
        self._cloud_progress_by_url: dict[str, float] = {}
        self._pdf_threads: list[PdfConvertThread] = []
        self._jwpub_threads: list[Any] = []
        self._lo_threads: list[LoConvertThread] = []
        self._linked_folder_files: dict[str, str] = {}  # file_path → node_id
        self._linked_folder_availability: tuple[tuple[str, bool], ...] = ()
        self._meeting_folder_imports: dict[str, dict[str, Any]] = {}
        self._meeting_folder_pending_sources: set[str] = set()
        self._connect_services()

    @property
    def thumb_cache(self) -> dict[str, QPixmap]:
        return self._thumb_cache

    @Property(str, notify=stateChanged)
    def playlistName(self):
        return self._playlist_name

    @Property(bool, notify=chromeChanged)
    def isTemp(self):
        return False

    @Property(bool, notify=chromeChanged)
    def isWatched(self):
        return False

    @Property(bool, notify=syncStateChanged)
    def syncAvailable(self):
        return self._sync_available

    @Property(bool, notify=syncStateChanged)
    def syncEnabled(self):
        return self._sync_enabled

    @Property(bool, notify=syncStateChanged)
    def syncBusy(self):
        return self._sync_busy

    @Property(str, notify=syncStateChanged)
    def syncFolderPath(self):
        return self._sync_folder

    @Property(bool, notify=chromeChanged)
    def hasItems(self):
        return bool(self._nodes)

    @Property(str, notify=chromeChanged)
    def itemCountText(self):
        count = count_media(self._nodes)
        word = _tr("_PlaylistEditView", "items")
        return f"{count} {word}"

    @Property("QVariant", notify=stateChanged)
    def playlistData(self):
        return self.tree_data()

    def placement_playlist_ref(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []

        def visit(children: list[Node], parent_section_id: str = "") -> None:
            for node in children:
                node_type = node.get("type", "")
                if node_type == "media":
                    items.append({"id": node.get("id", "")})
                    continue
                if node_type in ("section", "subsection"):
                    node_id = str(node.get("id", ""))
                    sections.append({
                        "id": node_id,
                        "name": node.get("title", ""),
                        "parent_id": parent_section_id or None,
                        "color_hue": int(node.get("color_hue", 215)),
                    })
                    visit(node.get("children", []), node_id)
                    continue
                visit(node.get("children", []), parent_section_id)

        visit(self._nodes)
        return {"items": items, "sections": sections}

    @Slot(str)
    def set_sync_root(self, watched_folder_path: str) -> None:
        self._sync_root = watched_folder_path or ""
        self._refresh_sync_availability()
        if self._tree_key:
            self._refresh_sync_from_manifest()

    def load_week(self, pub_type: str, wd: WeekData) -> None:
        self._meeting_type = pub_type
        if pub_type == "mwb":
            canonical = self._builder.build_midweek(wd)
            issue = wd.mwb_issue or ""
            self._playlist_name = wd.mwb_date_label or wd.mwb_week_title or _tr(
                "_PubCard", "Life & Ministry"
            )
        else:
            canonical = self._builder.build_weekend(wd)
            issue = wd.wt_issue or ""
            self._playlist_name = wd.wt_study_title or _tr(
                "_PubCard", "Watchtower Study"
            )
        self._tree_key = f"{pub_type}:{wd.monday.isoformat()}:{self._language_code}:{issue}"
        self._sync_identity = MeetingSyncIdentity(
            tree_key=self._tree_key,
            pub_type=pub_type,
            monday=wd.monday,
        )
        self._refresh_sync_availability()
        self._load_canonical(canonical)

    def load_memorial(self, md: MemorialData) -> None:
        canonical = self._builder.build_memorial(md)
        year = getattr(md, "year", 0) or (
            md.memorial_date.year if getattr(md, "memorial_date", None) else ""
        )
        date_key = md.memorial_date.isoformat() if md.memorial_date else str(year)
        self._playlist_name = _tr("_MemorialCard", "MEMORIAL")
        self._tree_key = f"memorial:{date_key}:{self._language_code}:{year}"
        self._sync_identity = None
        self._sync_enabled = False
        self._sync_folder = ""
        self._sync_revision = 0
        self._refresh_sync_availability()
        self._load_canonical(canonical)

    def _load_canonical(self, canonical: list[Node]) -> None:
        self._canonical_hash = self._builder.canonical_hash(canonical)
        if self._sync_identity is not None:
            self._sync_identity = MeetingSyncIdentity(
                tree_key=self._sync_identity.tree_key,
                pub_type=self._sync_identity.pub_type,
                monday=self._sync_identity.monday,
                canonical_hash=self._canonical_hash,
            )
        saved, _ = self._store.load(self._tree_key)
        self._deleted_source_keys = self._store.load_deleted_source_keys(self._tree_key)
        self._linked_folder_files = self._store.load_linked_folder_files(self._tree_key)
        self._meeting_folder_imports = self._store.load_meeting_folder_imports(self._tree_key)
        sync_record = self._load_sync_record()
        if sync_record is not None:
            saved = sync_record.nodes
            self._apply_sync_record(sync_record)
        else:
            self._sync_enabled = False
            self._sync_revision = 0
            self._sync_folder = self._candidate_sync_folder()
        self._meeting_folder_pending_sources.clear()
        self._nodes = MeetingTreeMerger(
            canonical,
            self._deleted_source_keys,
        ).merge(saved)
        self._linked_folder_availability = self._linked_folder_availability_signature()
        self._save()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        self.stateChanged.emit()

    def refresh_week(self, pub_type: str, wd: WeekData) -> None:
        self.load_week(pub_type, wd)

    def _refresh_sync_availability(self) -> None:
        available = bool(
            self._sync_root
            and Path(self._sync_root).is_dir()
            and self._sync_identity is not None
        )
        if available == self._sync_available:
            return
        self._sync_available = available
        if not available:
            self._sync_enabled = False
            self._sync_folder = ""
            self._sync_revision = 0
        self.syncStateChanged.emit()

    def _candidate_sync_folder(self) -> str:
        if not self._sync_root or self._sync_identity is None:
            return ""
        folder = self._sync_service.locate_folder(
            self._sync_root,
            self._sync_identity,
            create=False,
        )
        return str(folder) if folder else ""

    def _load_sync_record(self):
        if not self._sync_available or self._sync_identity is None:
            return None
        try:
            return self._sync_service.load_tree(self._sync_root, self._sync_identity)
        except ManifestError as exc:
            log_ignored_exception(__name__, "Meeting linked-folder manifest is invalid")
            QMessageBox.warning(
                self.parent(),
                _tr("MeetingSync", "Meeting sync"),
                str(exc),
            )
            return None
        except MeetingSyncError:
            log_ignored_exception(__name__, "Could not load meeting linked-folder sync")
            return None

    def _refresh_sync_from_manifest(self) -> bool:
        record = self._load_sync_record()
        if record is None:
            if self._sync_enabled:
                self._sync_enabled = False
                self._sync_revision = 0
                self._sync_folder = self._candidate_sync_folder()
                self.syncStateChanged.emit()
            return False
        if self._sync_enabled and record.revision == self._sync_revision:
            return True

        self._apply_sync_record(record)
        self._meeting_folder_pending_sources.clear()
        self._linked_folder_availability = self._linked_folder_availability_signature()
        self._save_local_cache()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        self.stateChanged.emit()
        return True

    def _apply_sync_record(self, record: MeetingSyncRecord) -> None:
        self._nodes = record.nodes
        self._deleted_source_keys = record.deleted_source_keys
        self._linked_folder_files = record.linked_folder_files
        self._meeting_folder_imports = record.meeting_folder_imports
        self._sync_folder = str(record.folder)
        self._sync_enabled = True
        self._sync_revision = record.revision

    # ── Meeting-folder autoimport ────────────────────────────────────────────

    def inject_linked_folder_media(self, watched_folder_path: str) -> None:
        """Scan meeting-targeted subfolders and import direct source files.

        - ``MW`` folders → ``lac`` section of the midweek tree
        - ``WE`` folders → ``public_talk`` section of the weekend tree

        Sources keep the same linked-folder meeting semantics as direct media:
        local items are tracked for availability and removal deletes their file.
        Processing state follows the active persistence target: local store when
        sync is off, linked-folder manifest when sync is on.
        """
        self.set_sync_root(watched_folder_path)
        if not watched_folder_path or not self._tree_key:
            return

        from ...core.ingest.watched_folder import (
            meeting_folder_source_needs_processing,
            scan_meeting_folder_sources,
        )

        # Determine which monday this tree belongs to
        # tree_key format: "<pub_type>:<monday>:<lang>:<issue>"
        parts = self._tree_key.split(":")
        if len(parts) < 2:
            return
        tree_monday = parts[1]  # ISO date string
        tree_pub_type = parts[0]  # "mwb" or "wt"

        # Map tag → pub_type
        tag_to_pub = {"MW": "mwb", "WE": "wt"}

        folders = scan_meeting_folder_sources(watched_folder_path)
        touched = False
        for folder in folders:
            # Only inject folders matching this tree's monday AND pub_type
            if folder["monday"] != tree_monday:
                continue
            expected_pub = tag_to_pub.get(folder["meeting_tag"], "")
            if expected_pub != tree_pub_type:
                continue

            target_list_id = self._meeting_folder_target_list_id(tree_pub_type)
            for source in folder.get("sources", []):
                if not self._meeting_folder_source_supported(source):
                    continue
                source_key = str(source.get("source_key") or "")
                if not source_key or source_key in self._meeting_folder_pending_sources:
                    continue
                record = self._meeting_folder_record_for_source(source)
                if not meeting_folder_source_needs_processing(source, record):
                    continue

                if not record:
                    adopted_ids = self._adopt_existing_meeting_folder_source(
                        source, str(folder.get("path") or "")
                    )
                    if adopted_ids:
                        self._record_meeting_folder_import(source, adopted_ids)
                        touched = True
                        continue

                source_with_folder = dict(source)
                source_with_folder["folder_path"] = str(folder.get("path") or "")
                self._remove_previous_meeting_folder_nodes(record)
                self._import_meeting_folder_source(source_with_folder, target_list_id, 0)
                touched = True

        self._emit_linked_folder_availability_if_changed()
        if touched:
            self.chromeChanged.emit()

    def _linked_folder_availability_signature(self) -> tuple[tuple[str, bool], ...]:
        """Snapshot local availability for linked-folder media nodes."""
        from ...core.ingest.watched_folder import local_file_availability_signature

        urls: list[str] = []
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            urls.append(self._url_for_node(node))
        return local_file_availability_signature(urls)

    def _emit_linked_folder_availability_if_changed(self) -> None:
        availability = self._linked_folder_availability_signature()
        if availability == self._linked_folder_availability:
            return
        self._linked_folder_availability = availability
        available_nodes: list[Node] = []
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            url = self._url_for_node(node)
            if not url or MediaCacheManager.is_remote(url) or os.path.exists(url):
                available_nodes.append(node)
        self._start_media_requests(available_nodes)
        self.stateChanged.emit()

    def _meeting_folder_target_list_id(self, tree_pub_type: str) -> str:
        target_section_code = "lac" if tree_pub_type == "mwb" else "public_talk"
        target_section = self._find_section_by_code(target_section_code)
        if target_section:
            return f"section:{target_section.get('id', '')}"
        return "root"

    def _meeting_folder_source_supported(self, source: dict[str, Any]) -> bool:
        kind = str(source.get("kind") or "")
        if kind in {"media", "pdf", "jwpub", "jwlplaylist"}:
            return True
        if kind == "lo":
            return libreoffice_available()
        return False

    def _meeting_folder_record_for_source(
        self,
        source: dict[str, Any],
    ) -> dict[str, Any] | None:
        source_key = str(source.get("source_key") or "")
        record = self._meeting_folder_imports.get(source_key)
        if isinstance(record, dict):
            return record

        source_path = str(source.get("path") or "")
        if not source_path:
            return None
        for existing in self._meeting_folder_imports.values():
            if not isinstance(existing, dict):
                continue
            if self._same_local_source(str(existing.get("path") or ""), source_path):
                return existing
        return None

    def _same_local_source(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a.startswith(("http://", "https://")) or b.startswith(("http://", "https://")):
            return a == b
        return (
            os.path.normcase(os.path.normpath(os.path.abspath(a)))
            == os.path.normcase(os.path.normpath(os.path.abspath(b)))
        )

    def _path_is_inside(self, path: str, folder: str) -> bool:
        try:
            Path(path).resolve().relative_to(Path(folder).resolve())
            return True
        except (OSError, ValueError):
            return False

    def _adopt_existing_meeting_folder_source(
        self, source: dict[str, Any], folder_path: str
    ) -> list[str]:
        """Treat an already-present direct media file as processed."""
        if source.get("kind") != "media":
            return []
        source_path = str(source.get("path") or "")
        adopted: list[str] = []
        changed = False
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media":
                continue
            if not self._same_local_source(self._url_for_node(node), source_path):
                continue
            node_id = str(node.get("id") or "")
            if node_id:
                adopted.append(node_id)
            if node.get("linked_folder_source") != folder_path:
                node["linked_folder_source"] = folder_path
                changed = True
            if node_id and self._linked_folder_files.get(source_path) != node_id:
                self._linked_folder_files[source_path] = node_id
                changed = True
        if changed:
            self._save()
            self.stateChanged.emit()
        return adopted

    def _remove_previous_meeting_folder_nodes(self, record: dict[str, Any] | None) -> None:
        if not isinstance(record, dict):
            return
        removed = False
        for node_id in list(record.get("node_ids", [])):
            if node_id and self._replace_node(str(node_id), []):
                removed = True
        if removed:
            self._save()
            self.stateChanged.emit()
            self._emit_section_counts()

    def _record_meeting_folder_import(
        self,
        source: dict[str, Any],
        node_ids: list[str] | None = None,
        *,
        status: str = "processed",
        error: str = "",
    ) -> None:
        source_key = str(source.get("source_key") or "")
        if not source_key:
            return
        record: dict[str, Any] = {
            "source_key": source_key,
            "path": str(source.get("path") or ""),
            "name": str(source.get("name") or ""),
            "kind": str(source.get("kind") or ""),
            "signature": dict(source.get("signature") or {}),
            "status": status,
            "node_ids": list(node_ids or []),
        }
        if error:
            record["error"] = str(error)[:500]
        self._forget_meeting_folder_records_for_path(source_key, record["path"])
        self._meeting_folder_imports[source_key] = record
        self._meeting_folder_pending_sources.discard(source_key)
        self._save()

    def _forget_meeting_folder_records_for_path(
        self,
        source_key: str,
        source_path: str,
    ) -> None:
        if not source_path:
            return
        for key, record in list(self._meeting_folder_imports.items()):
            if key == source_key:
                continue
            if not isinstance(record, dict):
                continue
            if self._same_local_source(str(record.get("path") or ""), source_path):
                self._meeting_folder_imports.pop(key, None)

    def _record_meeting_folder_failure(
        self, source: dict[str, Any], name: str, error: str
    ) -> None:
        self._record_meeting_folder_import(
            source,
            [],
            status="failed",
            error=error,
        )
        self._warn_import_failed(name, error)

    def _page_nodes(self, pages: list[str], stem: str) -> list[Node]:
        return [
            self._manual_media_node(page, title=f"{stem} - p. {idx + 1}")
            for idx, page in enumerate(pages)
        ]

    def _insert_meeting_folder_nodes(
        self,
        source: dict[str, Any],
        nodes: list[Node],
        list_id: str,
        insert_index: int,
    ) -> None:
        folder_path = str(source.get("folder_path") or "")
        linked_files: dict[str, str] = {}
        if folder_path:
            for node in nodes:
                node["linked_folder_source"] = folder_path
                url = self._url_for_node(node)
                node_id = str(node.get("id") or "")
                if url and node_id:
                    linked_files[url] = node_id
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)
        if linked_files:
            self._linked_folder_files.update(linked_files)
        self._record_meeting_folder_import(
            source,
            [str(node.get("id") or "") for node in nodes if node.get("id")],
        )

    def _import_meeting_folder_source(
        self,
        source: dict[str, Any],
        list_id: str,
        insert_index: int,
    ) -> None:
        source_key = str(source.get("source_key") or "")
        path = str(source.get("path") or "")
        kind = str(source.get("kind") or "")
        if not source_key or not path:
            return
        self._meeting_folder_pending_sources.add(source_key)

        if kind == "media":
            self._insert_meeting_folder_nodes(
                source, [self._manual_media_node(path)], list_id, insert_index
            )
            return
        if kind == "pdf":
            self._import_meeting_folder_pdf(source, list_id, insert_index)
            return
        if kind == "lo":
            self._import_meeting_folder_lo(source, list_id, insert_index)
            return
        if kind == "jwpub":
            self._import_meeting_folder_jwpub(source, list_id, insert_index)
            return
        if kind == "jwlplaylist":
            self._import_meeting_folder_jwlplaylist(source, list_id, insert_index)
            return
        self._meeting_folder_pending_sources.discard(source_key)

    def _import_meeting_folder_pdf(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        stem = Path(path).stem
        pages = pdf_cached_pages(path)
        if pages:
            self._insert_meeting_folder_nodes(
                source, self._page_nodes(pages, stem), list_id, insert_index
            )
            return
        thread = PdfConvertThread(path, parent=self)
        self._pdf_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, pdf_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, pdf_stem), _list, _index
                )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t)
            if t in self._pdf_threads else None
        )
        thread.start()

    def _import_meeting_folder_lo(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        stem = Path(path).stem
        pages = lo_cached_pages(path)
        if pages:
            self._insert_meeting_folder_nodes(
                source, self._page_nodes(pages, stem), list_id, insert_index
            )
            return
        thread = LoConvertThread(path, parent=self)
        self._lo_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, doc_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, doc_stem), _list, _index
                )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._lo_threads.remove(t)
            if t in self._lo_threads else None
        )
        thread.start()

    def _import_meeting_folder_jwpub(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        from ...core.jw.publication_reader import JwpubImportThread

        path = str(source.get("path") or "")
        stem = Path(path).stem
        thread = JwpubImportThread.create(
            path,
            lang=self._language_code,
            dest_images_dir=_paths.IMAGES_DIR,
            parent=self,
        )
        self._jwpub_threads.append(thread)
        thread.items_ready.connect(
            lambda items, file_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source,
                    [self._node_from_playlist_item(raw, file_stem) for raw in items],
                    _list,
                    _index,
                )
        )
        thread.failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t)
            if t in self._jwpub_threads else None
        )
        thread.start()

    def _import_meeting_folder_jwlplaylist(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        from ...core.playlists.reader import read_jwlplaylist

        path = str(source.get("path") or "")
        try:
            data = read_jwlplaylist(
                path,
                fallback_lang_code=self._fallback_language_code,
            )
            nodes = [
                self._node_from_playlist_item(raw, Path(path).stem)
                for raw in data.get("items", [])
            ]
        except (OSError, ValueError) as exc:
            self._record_meeting_folder_failure(source, Path(path).name, str(exc))
            return
        self._insert_meeting_folder_nodes(source, nodes, list_id, insert_index)

    def _find_section_by_code(self, section_code: str) -> Node | None:
        """Find a section node by its ``section_code`` attribute."""
        for node in iter_nodes(self._nodes):
            if node.get("type") in ("section", "subsection"):
                if node.get("section_code") == section_code:
                    return node
                # Also match by meeting_source_key pattern
                source_key = node.get("meeting_source_key", "")
                if source_key.endswith(f":{section_code}"):
                    return node
        return None

    def tree_data(self) -> list[Node]:
        return [self._qml_node(node) for node in self._nodes]

    def cleanup(self) -> None:
        try:
            self._info_queue.info_ready.disconnect(self._on_info_ready)
            self._info_queue.duration_ready.disconnect(self._on_duration_ready)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree info queue")
        try:
            self._svc.video_resolved.disconnect(self._on_video_resolved)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree video resolver")
        try:
            mgr = MediaCacheManager.instance()
            mgr.cache_changed.disconnect(self._on_cache_changed)
            mgr.cache_removed.disconnect(self._on_cache_removed)
            mgr.prefetch_progress.disconnect(self._on_prefetch_progress)
            mgr.prefetch_error.disconnect(self._on_prefetch_error)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree cache signals")

    def disk_thumbnail(self, item_id: str) -> QPixmap | None:
        node = self._find_node(item_id)
        path = self._thumbnail_local_path(node, item_id)
        if os.path.exists(path):
            px = QPixmap(path)
            if not px.isNull():
                return px
        return None

    def add_files(self, paths: list[str], list_id: str = "root",
                  insert_index: int = _BIG_INDEX) -> None:
        media_paths = []
        pdf_paths = []
        jwpub_paths = []
        jwl_paths = []
        lo_paths = []
        for path in paths:
            ext = Path(path).suffix.lower() if path else ""
            if ext in MEDIA_EXTS:
                media_paths.append(path)
            elif ext in PDF_EXTS:
                pdf_paths.append(path)
            elif ext in JWPUB_EXTS:
                jwpub_paths.append(path)
            elif ext in PLAYLIST_EXTS:
                jwl_paths.append(path)
            elif ext in (PPTX_EXTS | DOCX_EXTS) and libreoffice_available():
                lo_paths.append(path)

        cursor = insert_index
        if media_paths:
            nodes = [self._manual_media_node(path) for path in media_paths]
            self._insert_nodes(list_id or "root", cursor, nodes)
            if cursor < _BIG_INDEX:
                cursor += len(nodes)
        if pdf_paths:
            self._import_pdfs(pdf_paths, list_id or "root", cursor)
        if jwpub_paths:
            self._import_jwpubs(jwpub_paths, list_id or "root", cursor)
        if jwl_paths:
            added = self._import_jwlplaylists(jwl_paths, list_id or "root", cursor)
            if cursor < _BIG_INDEX:
                cursor += added
        if lo_paths:
            self._import_lo_files(lo_paths, list_id or "root", cursor)

    def add_from_jw_catalog(
        self,
        item_data: dict[str, Any],
        list_id: str = "root",
        insert_index: int = _BIG_INDEX,
    ) -> None:
        node_id = new_node_id()
        track = self._to_int(item_data.get("track"))
        issue = self._to_int(item_data.get("issue"))
        doc_id = self._to_int(item_data.get("docid"))
        ref = {
            "multimedia_id": 0,
            "mime_type": "video/mp4",
            "file_path": item_data.get("download_url", ""),
            "label": item_data.get("title", ""),
            "caption": "",
            "begin_ordinal": 0,
            "key_symbol": item_data.get("pub") or "",
            "track": track,
            "issue_tag": issue,
            "meps_doc_id": doc_id,
            "section": "",
            "is_song": False,
            "cbs_article_title": "",
        }
        node = {
            "id": node_id,
            "type": "media",
            "title": ref["label"] or _tr("_MediaRow", "Media"),
            "media_type": "video",
            "media_ref": ref,
            "children": [],
            "meeting_generated": False,
        }
        thumb_path = item_data.get("thumbnail_path", "")
        if thumb_path and os.path.exists(thumb_path):
            try:
                target_path = meeting_thumb_path(node_id)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target = os.fspath(target_path)
                shutil.copy2(thumb_path, target)
                node["thumbnail_cache_key"] = meeting_thumb_cache_key(node_id)
                node["thumbnail_local_path"] = target
            except OSError:
                log_ignored_exception(__name__, "Could not copy meeting item thumbnail")
        self._insert_nodes(list_id or "root", insert_index, [node])

    def _import_pdfs(self, paths: list[str], list_id: str, insert_index: int) -> None:
        for path in paths:
            stem = Path(path).stem
            pages = pdf_cached_pages(path)
            if pages:
                self._on_pdf_pages_ready(pages, stem, list_id, insert_index)
                if insert_index < _BIG_INDEX:
                    insert_index += len(pages)
                continue
            thread = PdfConvertThread(path, parent=self)
            self._pdf_threads.append(thread)
            thread.pages_ready.connect(
                lambda pages, pdf_stem, _list=list_id, _index=insert_index:
                    self._on_pdf_pages_ready(pages, pdf_stem, _list, _index)
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._pdf_threads.remove(t)
                if t in self._pdf_threads else None
            )
            thread.start()

    def _on_pdf_pages_ready(
        self,
        pages: list[str],
        pdf_stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [
            self._manual_media_node(page, title=f"{pdf_stem} - p. {idx + 1}")
            for idx, page in enumerate(pages)
        ]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _import_lo_files(self, paths: list[str], list_id: str, insert_index: int) -> None:
        for path in paths:
            stem = Path(path).stem
            pages = lo_cached_pages(path)
            if pages:
                self._on_lo_pages_ready(pages, stem, list_id, insert_index)
                if insert_index < _BIG_INDEX:
                    insert_index += len(pages)
                continue
            thread = LoConvertThread(path, parent=self)
            self._lo_threads.append(thread)
            thread.pages_ready.connect(
                lambda pages, doc_stem, _list=list_id, _index=insert_index:
                    self._on_lo_pages_ready(pages, doc_stem, _list, _index)
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._lo_threads.remove(t)
                if t in self._lo_threads else None
            )
            thread.start()

    def _on_lo_pages_ready(
        self,
        pages: list[str],
        stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [
            self._manual_media_node(page, title=f"{stem} - p. {idx + 1}")
            for idx, page in enumerate(pages)
        ]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _import_jwpubs(self, paths: list[str], list_id: str, insert_index: int) -> None:
        from ...core.jw.publication_reader import JwpubImportThread

        for path in paths:
            stem = Path(path).stem
            thread = JwpubImportThread.create(
                path,
                lang=self._language_code,
                dest_images_dir=self._jwpub_image_dir(),
                parent=self,
            )
            self._jwpub_threads.append(thread)
            thread.items_ready.connect(
                lambda items, file_stem, _list=list_id, _index=insert_index:
                    self._on_playlist_items_ready(items, file_stem, _list, _index)
            )
            thread.failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._jwpub_threads.remove(t)
                if t in self._jwpub_threads else None
            )
            thread.start()

    def _import_jwlplaylists(self, paths: list[str], list_id: str, insert_index: int) -> int:
        from ...core.playlists.reader import read_jwlplaylist

        total = 0
        for path in paths:
            try:
                data = read_jwlplaylist(
                    path,
                    fallback_lang_code=self._fallback_language_code,
                )
                nodes = [
                    self._node_from_playlist_item(raw, Path(path).stem)
                    for raw in data.get("items", [])
                ]
            except (OSError, ValueError) as exc:
                self._warn_import_failed(Path(path).name, str(exc))
                continue
            if nodes:
                self._insert_nodes(list_id or "root", insert_index, nodes)
                total += len(nodes)
                if insert_index < _BIG_INDEX:
                    insert_index += len(nodes)
        return total

    def _on_playlist_items_ready(
        self,
        items: list[dict[str, Any]],
        file_stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [self._node_from_playlist_item(raw, file_stem) for raw in items]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _node_from_playlist_item(self, raw: dict[str, Any], fallback_title: str) -> Node:
        node_id = new_node_id()
        url = str(raw.get("url") or raw.get("jworg_url") or "")
        if raw.get("data") and not url:
            embedded_dir = self._embedded_media_dir()
            os.makedirs(embedded_dir, exist_ok=True)
            ext = Path(str(raw.get("filename") or "media")).suffix or ".mp4"
            url = os.path.join(embedded_dir, f"{node_id}{ext}")
            with open(url, "wb") as handle:
                handle.write(raw["data"])
        media_type = str(raw.get("type") or "").lower()
        if media_type not in ("image", "audio", "video"):
            media_type = _media_type_from_path(url)
        title = _clean_title(str(raw.get("title") or fallback_title or Path(url).stem))
        ref = {
            "multimedia_id": self._to_int(raw.get("multimedia_id")),
            "mime_type": str(raw.get("mime_type") or _mime_for(url, media_type)),
            "file_path": url,
            "label": title,
            "caption": "",
            "begin_ordinal": 0,
            "key_symbol": raw.get("key_symbol") or raw.get("pub") or "",
            "track": self._to_int(raw.get("track")),
            "issue_tag": self._to_int(raw.get("issue_tag") or raw.get("issue")),
            "meps_doc_id": self._to_int(raw.get("doc_id") or raw.get("meps_doc_id")),
            "section": "",
            "is_song": False,
            "cbs_article_title": "",
        }
        return {
            "id": node_id,
            "type": "media",
            "title": title or _tr("_MediaRow", "Media"),
            "media_type": media_type,
            "media_ref": ref,
            "children": [],
            "meeting_generated": False,
            "auto_title": bool(raw.get("auto_title", False)),
        }

    def _warn_import_failed(self, name: str, error: str) -> None:
        parent = self.parent()
        QMessageBox.warning(
            parent,
            _tr("_PlaylistEditView", "Add Media"),
            f"{name}\n{str(error)[:160]}",
        )

    @Slot()
    def backClicked(self):
        self.backRequested.emit()

    @Slot()
    def addClicked(self):
        parent = self.parent()
        filter_text = (
            "Media, PDF & Publication "
            "(*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.wav "
            "*.jpg *.jpeg *.png *.gif *.webp *.pdf *.jwpub *.jwlplaylist);;"
            "JW Playlist (*.jwlplaylist);;"
            "JW Publication (*.jwpub);;"
            "PDF (*.pdf);;"
            "Video (*.mp4 *.mkv *.mov *.avi *.webm);;"
            "Audio (*.mp3 *.m4a *.wav);;"
            "Image (*.jpg *.jpeg *.png *.gif *.webp);;"
            "All (*)"
        )
        paths, _ = QFileDialog.getOpenFileNames(
            parent,
            _tr("_PlaylistEditView", "Add Media"),
            "",
            filter_text,
        )
        if paths:
            self.add_files(paths)

    @Slot()
    def toggleSync(self) -> None:
        if self._sync_busy:
            return
        if self._sync_enabled:
            self._disable_sync()
        else:
            self._enable_sync()

    def _enable_sync(self) -> None:
        if not self._sync_available or self._sync_identity is None:
            return
        old_state = self._sync_snapshot()
        self._set_sync_busy(True)
        try:
            folder = self._sync_service.locate_folder(
                self._sync_root,
                self._sync_identity,
                create=True,
            )
            if folder is None:
                raise MeetingSyncError("Linked folder is not available.")
            record = self._sync_service.load_tree(self._sync_root, self._sync_identity)
            self._sync_folder = str(folder)
            self._sync_enabled = True
            self._sync_revision = record.revision if record is not None else 0
            if record is not None:
                self._apply_sync_record(record)
                self._save_local_cache()
                self._linked_folder_availability = self._linked_folder_availability_signature()
                self.chromeChanged.emit()
                self.stateChanged.emit()
                return
            self._materialize_current_nodes_for_sync()
            saved_record = self._sync_service.save_tree(
                folder,
                self._sync_identity,
                nodes=self._nodes,
                deleted_source_keys=self._deleted_source_keys,
                linked_folder_files=self._linked_folder_files,
                meeting_folder_imports=self._meeting_folder_imports,
                expected_revision=self._sync_revision,
            )
            self._apply_sync_record(saved_record)
            self._save_local_cache()
            self._linked_folder_availability = self._linked_folder_availability_signature()
            self.chromeChanged.emit()
            self.stateChanged.emit()
        except (ManifestError, MeetingSyncError, OSError) as exc:
            self._restore_sync_snapshot(old_state)
            self._save_local_cache()
            self._warn_sync_failed(str(exc))
        finally:
            self._set_sync_busy(False)
            self.syncStateChanged.emit()

    def _disable_sync(self) -> None:
        if not self._sync_folder:
            return
        reply = QMessageBox.question(
            self.parent(),
            _tr("MeetingSync", "Meeting sync"),
            _tr(
                "MeetingSync",
                "Turn off sync for this meeting?\nThe folder files will be kept.",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        old_state = self._sync_snapshot()
        self._set_sync_busy(True)
        folder = Path(self._sync_folder)
        try:
            record = self._sync_service.load_tree(self._sync_root, self._sync_identity) if self._sync_identity else None
            if record is not None:
                self._nodes = record.nodes
                self._deleted_source_keys = record.deleted_source_keys
                self._meeting_folder_imports = record.meeting_folder_imports
            durable_dir = self._durable_detached_dir()
            self._nodes = self._sync_service.detach_cache_references(
                self._nodes,
                folder,
                durable_dir,
            )
            self._linked_folder_files = self._linked_files_for_current_nodes()
            self._sync_enabled = False
            self._sync_revision = 0
            self._save_local_cache()
            self._sync_service.delete_sync_metadata(folder)
            self._sync_folder = str(folder)
            self._linked_folder_availability = self._linked_folder_availability_signature()
            if self._sync_root:
                self.inject_linked_folder_media(self._sync_root)
            self.chromeChanged.emit()
            self.stateChanged.emit()
        except (ManifestError, MeetingSyncError, OSError) as exc:
            self._restore_sync_snapshot(old_state)
            self._warn_sync_failed(str(exc))
        finally:
            self._set_sync_busy(False)
            self.syncStateChanged.emit()

    def _sync_snapshot(self) -> dict[str, Any]:
        return {
            "nodes": clone_nodes(self._nodes),
            "deleted": set(self._deleted_source_keys),
            "linked": dict(self._linked_folder_files),
            "imports": copy.deepcopy(self._meeting_folder_imports),
            "folder": self._sync_folder,
            "enabled": self._sync_enabled,
            "revision": self._sync_revision,
        }

    def _restore_sync_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._nodes = snapshot["nodes"]
        self._deleted_source_keys = snapshot["deleted"]
        self._linked_folder_files = snapshot["linked"]
        self._meeting_folder_imports = snapshot["imports"]
        self._sync_folder = snapshot["folder"]
        self._sync_enabled = snapshot["enabled"]
        self._sync_revision = snapshot["revision"]

    def _set_sync_busy(self, value: bool) -> None:
        if self._sync_busy == value:
            return
        self._sync_busy = value
        self.syncStateChanged.emit()

    def _durable_detached_dir(self) -> Path:
        base = getattr(_paths, "EMBEDDED_DIR", "")
        if base:
            return Path(base) / "meeting_sync"
        if self._sync_folder:
            return Path(self._sync_folder).parent / ".solin_detached_meeting_sync"
        return Path.cwd() / ".solin_detached_meeting_sync"

    def _warn_sync_failed(self, message: str) -> None:
        QMessageBox.warning(
            self.parent(),
            _tr("MeetingSync", "Meeting sync"),
            message[:500] or _tr("MeetingSync", "Could not update meeting sync."),
        )

    @Slot()
    def newSectionClicked(self):
        dlg = _NameDialog(
            parent=self.parent(),
            label=_tr("_PlaylistEditView", "Section name:"),
            placeholder=_tr("_PlaylistEditView", "E.g.: Introduction"),
        )
        dlg.setWindowTitle(_tr("_PlaylistEditView", "New Section"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        hues = [int(n.get("color_hue", 0)) for n in iter_nodes(self._nodes)]
        node = {
            "id": new_node_id(),
            "type": "section",
            "title": name,
            "color_hue": generate_section_hue(hues),
            "collapsed": False,
            "children": [],
            "meeting_generated": False,
        }
        self._insert_nodes("root", _BIG_INDEX, [node], signal_name="nodes")

    @Slot(str)
    def newSubsectionClicked(self, parent_section_id: str):
        parent_node = self._find_node(parent_section_id)
        if not parent_node or parent_node.get("type") != "section":
            return
        dlg = _NameDialog(
            parent=self.parent(),
            label=_tr("_PlaylistEditView", "Subsection name:"),
            placeholder=_tr("_PlaylistEditView", "E.g.: Part 1"),
        )
        dlg.setWindowTitle(_tr("_PlaylistEditView", "New Subsection"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node = {
            "id": new_node_id(),
            "type": "subsection",
            "title": name,
            "color_hue": parent_node.get("color_hue", 215),
            "collapsed": False,
            "children": [],
            "meeting_generated": False,
        }
        self._insert_nodes(f"section:{parent_section_id}", _BIG_INDEX, [node], signal_name="nodes")

    @Slot(str)
    def newMarkerClicked(self, subsection_id: str):
        parent_node = self._find_node(subsection_id)
        if not parent_node or parent_node.get("type") != "subsection":
            return
        node = {
            "id": new_node_id(),
            "type": "marker",
            "text": "",
            "children": [],
            "meeting_generated": False,
        }
        if parent_node.get("collapsed"):
            parent_node["collapsed"] = False
            self._insert_nodes(f"subsection:{subsection_id}", 0, [node], signal_name="state")
        else:
            self._insert_nodes(f"subsection:{subsection_id}", 0, [node], signal_name="nodes")
        self.markerEditRequested.emit(node["id"])

    @Slot(str)
    def projectItem(self, item_id: str):
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        ref = copy.deepcopy(node.get("media_ref") or {})
        title = node.get("title", "")
        if title:
            ref["label"] = title
        resolved = self._resolved_urls.get(item_id) or node.get("resolved_url", "")
        if resolved:
            ref["file_path"] = resolved
        self.projectRequested.emit(_meeting_media_from_ref(ref))

    @Slot(str)
    def removeItem(self, item_id: str):
        node = self._find_node(item_id)
        if not node:
            return
        # If this item came from a linked folder, physically delete the file
        linked_source = node.get("linked_folder_source", "")
        if linked_source:
            file_path = self._url_for_node(node)
            removed_physical_file = bool(file_path and not os.path.exists(file_path))
            if (
                file_path
                and os.path.isfile(file_path)
                and self._path_is_inside(file_path, linked_source)
            ):
                try:
                    os.remove(file_path)
                    removed_physical_file = True
                except OSError:
                    pass
            # Remove from tracking
            self._linked_folder_files.pop(file_path, None)
            self._cleanup_meeting_folder_import_for_removed_node(
                item_id,
                file_path,
                source_removed=removed_physical_file,
            )
        self._remember_deleted_sources(node, include_media=True)
        if self._replace_node(item_id, []):
            self._save_and_emit_replace(item_id, [])

    def _cleanup_meeting_folder_import_for_removed_node(
        self,
        node_id: str,
        file_path: str,
        *,
        source_removed: bool,
    ) -> None:
        if not node_id and not file_path:
            return
        for key, record in list(self._meeting_folder_imports.items()):
            if not isinstance(record, dict):
                continue
            record_path = str(record.get("path") or "")
            node_ids = [
                str(value)
                for value in record.get("node_ids", [])
                if value
            ]
            contains_node = bool(node_id and node_id in node_ids)
            same_source_file = bool(
                file_path
                and record_path
                and self._same_local_source(record_path, file_path)
            )
            source_is_gone = bool(
                record_path
                and same_source_file
                and (source_removed or not os.path.exists(record_path))
            )
            if source_is_gone:
                self._meeting_folder_imports.pop(key, None)
                continue
            if contains_node:
                remaining = [value for value in node_ids if value != node_id]
                if remaining != node_ids:
                    record["node_ids"] = remaining

    @Slot(str)
    def renameItem(self, item_id: str):
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        dlg = _NameDialog(node.get("title", ""), parent=self.parent())
        dlg.setWindowTitle(_tr("_PlaylistEditView", "Rename media"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node["title"] = name
        node["user_title_override"] = True
        ref = node.setdefault("media_ref", {})
        ref["label"] = name
        self._save()
        self.mediaChanged.emit(item_id, name, self._duration_for(node), self._thumb_source_for(item_id))

    @Slot(str)
    def downloadItem(self, item_id: str):
        url = self._url_for_node_id(item_id)
        if not url:
            return
        mgr = MediaCacheManager.instance()
        if not mgr.is_cached(url) and not mgr.is_prefetching(url):
            mgr.prefetch(url, priority=True)
            self._emit_cloud_for_node(item_id)

    @Slot(str, str)
    def renameMarker(self, marker_id: str, text: str):
        node = self._find_node(marker_id)
        if not node or node.get("type") != "marker":
            return
        node["text"] = text
        node["user_title_override"] = True
        self._save()

    @Slot(str)
    def deleteMarker(self, marker_id: str):
        node = self._find_node(marker_id)
        if node:
            self._remember_deleted_sources(node, include_media=False)
        if self._replace_node(marker_id, []):
            self._save_and_emit_replace(marker_id, [])

    @Slot(str)
    def renameSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        label = (
            _tr("_PlaylistEditView", "Subsection name:")
            if node.get("type") == "subsection"
            else _tr("_PlaylistEditView", "Section name:")
        )
        dlg = _NameDialog(node.get("title", ""), parent=self.parent(), label=label)
        dlg.setWindowTitle(_tr("_PlaylistEditView", "Rename section"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node["title"] = name
        node["user_title_override"] = True
        self._save()
        self.stateChanged.emit()

    @Slot(str)
    def deleteSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        reply = QMessageBox.question(
            self.parent(),
            _tr("_PlaylistEditView", "Delete section"),
            _tr("_PlaylistEditView", 'Delete section "{name}"?\nItems inside will be kept.').replace(
                "{name}", str(node.get("title", ""))
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remember_deleted_sources(node, include_media=False)
        replacement = self._media_descendants(node)
        if self._replace_node(section_id, replacement):
            self._save_and_emit_replace(section_id, replacement)

    @Slot(str)
    def recolorSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        dlg = _HuePickerDialog(int(node.get("color_hue", 215)), parent=self.parent())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        node["color_hue"] = dlg.selected_hue()
        self._save()
        self.stateChanged.emit()

    @Slot(str)
    def toggleCollapse(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        node["collapsed"] = not bool(node.get("collapsed", False))
        self._save()

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()

    @Slot(str, str, str, result=bool)
    def canDrop(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        kind, _ = self._parse_list_id(target_list_id)
        if kind == "root":
            return node_type in ("media", "section")
        if kind == "section":
            return node_type in ("media", "subsection")
        if kind == "subsection":
            return node_type in ("media", "marker")
        return False

    @Slot(str, str, int, result=bool)
    def moveNode(self, node_id: str, target_list_id: str, insert_index: int) -> bool:
        source, parent_children, original_index = self._pop_node_with_parent(node_id)
        if source is None or parent_children is None or original_index < 0:
            return False
        if not self.canDrop(node_id, source.get("type", ""), target_list_id):
            parent_children.insert(original_index, source)
            return False
        kind, target_id = self._parse_list_id(target_list_id)
        if target_id and self._contains_node(source, target_id):
            parent_children.insert(original_index, source)
            return False
        if not self._insert_existing_node(source, kind, target_id, insert_index):
            parent_children.insert(original_index, source)
            return False
        self._save()
        self.chromeChanged.emit()
        self._emit_section_counts()
        return True

    def _connect_services(self) -> None:
        self._svc.video_resolved.connect(self._on_video_resolved)
        mgr = MediaCacheManager.instance()
        mgr.cache_changed.connect(self._on_cache_changed)
        mgr.cache_removed.connect(self._on_cache_removed)
        mgr.prefetch_progress.connect(self._on_prefetch_progress)
        mgr.prefetch_error.connect(self._on_prefetch_error)

    def _save(self) -> None:
        if not self._tree_key:
            return
        if self._sync_enabled:
            self._materialize_current_nodes_for_sync()
            self._save_sync_manifest()
        self._save_local_cache()

    def _save_local_cache(self) -> None:
        if not self._tree_key:
            return
        self._store.save(
            self._tree_key,
            self._nodes,
            self._canonical_hash,
            self._deleted_source_keys,
            self._linked_folder_files or None,
            self._meeting_folder_imports or None,
        )

    def _save_sync_manifest(self) -> None:
        if not self._sync_identity or not self._sync_folder:
            return
        try:
            saved_record = self._sync_service.save_tree(
                Path(self._sync_folder),
                self._sync_identity,
                nodes=self._nodes,
                deleted_source_keys=self._deleted_source_keys,
                linked_folder_files=self._linked_folder_files,
                meeting_folder_imports=self._meeting_folder_imports,
                expected_revision=self._sync_revision,
            )
            self._apply_sync_record(saved_record)
        except (ManifestError, MeetingSyncError) as exc:
            log_ignored_exception(__name__, "Could not save meeting sync manifest")
            self._pause_sync_after_save_failure(str(exc))

    def _pause_sync_after_save_failure(self, message: str) -> None:
        self._sync_enabled = False
        self._sync_revision = 0
        self.syncStateChanged.emit()
        self._warn_sync_failed(message)

    def _materialize_current_nodes_for_sync(self) -> None:
        if not self._sync_folder:
            return
        self._nodes, linked_files = self._sync_service.materialize_tree_files(
            self._nodes,
            Path(self._sync_folder),
            generated_roots=self._generated_asset_roots(),
        )
        if linked_files:
            self._linked_folder_files.update(linked_files)

    def _linked_files_for_current_nodes(self) -> dict[str, str]:
        linked: dict[str, str] = {}
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            node_id = str(node.get("id") or "")
            url = self._url_for_node(node)
            if node_id and url:
                linked[url] = node_id
        return linked

    def _prepare_nodes_for_sync(self, nodes: list[Node]) -> list[Node]:
        if not self._sync_enabled or not self._sync_folder or not nodes:
            return nodes
        prepared, linked_files = self._sync_service.materialize_tree_files(
            nodes,
            Path(self._sync_folder),
            generated_roots=self._generated_asset_roots(),
        )
        if linked_files:
            self._linked_folder_files.update(linked_files)
        return prepared

    def _jwpub_image_dir(self) -> str:
        if self._sync_enabled and self._sync_folder:
            return str(cache_dir(Path(self._sync_folder)))
        return _paths.IMAGES_DIR

    def _embedded_media_dir(self) -> str:
        if self._sync_enabled and self._sync_folder:
            return str(cache_dir(Path(self._sync_folder)))
        return _paths.EMBEDDED_DIR

    def _generated_asset_roots(self) -> tuple[str, ...]:
        return tuple(
            root
            for root in (
                getattr(_paths, "CACHE_DIR", ""),
                getattr(_paths, "IMAGES_DIR", ""),
                getattr(_paths, "EMBEDDED_DIR", ""),
                os.fspath(meeting_thumb_dir()),
            )
            if root
        )

    def _manual_media_node(self, path: str, title: str = "") -> Node:
        media_type = _media_type_from_path(path)
        title = _clean_title(title) or Path(path).stem
        node_id = new_node_id()
        ref = {
            "multimedia_id": 0,
            "mime_type": _mime_for(path, media_type),
            "file_path": path,
            "label": title,
            "caption": "",
            "begin_ordinal": 0,
            "key_symbol": "",
            "track": 0,
            "issue_tag": 0,
            "meps_doc_id": 0,
            "section": "",
            "is_song": False,
            "cbs_article_title": "",
        }
        return {
            "id": node_id,
            "type": "media",
            "title": title,
            "media_type": media_type,
            "media_ref": ref,
            "children": [],
            "meeting_generated": False,
        }

    def _insert_nodes(
        self,
        list_id: str,
        insert_index: int,
        nodes: list[Node],
        *,
        signal_name: str = "media",
    ) -> None:
        kind, target_id = self._parse_list_id(list_id)
        target_children = self._children_for_target(kind, target_id)
        if target_children is None:
            return
        nodes = self._prepare_nodes_for_sync(nodes)
        index = max(0, min(insert_index, len(target_children)))
        for offset, node in enumerate(nodes):
            target_children.insert(index + offset, node)
        self._save()
        self._start_media_requests(nodes)
        self.chromeChanged.emit()
        qml_nodes = [self._qml_node(node) for node in nodes]
        if signal_name == "state":
            self.stateChanged.emit()
        elif signal_name == "nodes":
            self.nodesInserted.emit(list_id, index, qml_nodes)
        else:
            self.mediaInserted.emit(list_id, index, qml_nodes)
        self._emit_section_counts()

    def _children_for_target(self, kind: str, node_id: str) -> list[Node] | None:
        if kind == "root":
            return self._nodes
        target = self._find_node(node_id)
        if not target or target.get("type") != kind:
            return None
        return target.setdefault("children", [])

    def _insert_existing_node(
        self,
        node: Node,
        target_kind: str,
        target_id: str,
        insert_index: int,
    ) -> bool:
        target_children = self._children_for_target(target_kind, target_id)
        if target_children is None:
            return False
        index = max(0, min(insert_index, len(target_children)))
        target_children.insert(index, node)
        return True

    def _parse_list_id(self, list_id: str) -> tuple[str, str]:
        if list_id == "root" or not list_id:
            return "root", ""
        if ":" not in list_id:
            return "", ""
        return tuple(list_id.split(":", 1))  # type: ignore[return-value]

    def _find_node(self, node_id: str, nodes: list[Node] | None = None) -> Node | None:
        for node in self._nodes if nodes is None else nodes:
            if node.get("id") == node_id:
                return node
            found = self._find_node(node_id, node.get("children", []))
            if found:
                return found
        return None

    def _pop_node_with_parent(self, node_id: str) -> tuple[Node | None, list[Node] | None, int]:
        def visit(children: list[Node]) -> tuple[Node | None, list[Node] | None, int]:
            for idx, child in enumerate(children):
                if child.get("id") == node_id:
                    return children.pop(idx), children, idx
                found, parent, index = visit(child.get("children", []))
                if found is not None:
                    return found, parent, index
            return None, None, -1
        return visit(self._nodes)

    def _replace_node(self, node_id: str, replacement: list[Node]) -> bool:
        def visit(children: list[Node]) -> bool:
            for idx, child in enumerate(children):
                if child.get("id") == node_id:
                    children[idx:idx + 1] = replacement
                    return True
                if visit(child.get("children", [])):
                    return True
            return False
        return visit(self._nodes)

    def _contains_node(self, node: Node, target_id: str) -> bool:
        if not target_id:
            return False
        if node.get("id") == target_id:
            return True
        return any(self._contains_node(child, target_id)
                   for child in node.get("children", []))

    def _media_descendants(self, node: Node) -> list[Node]:
        result: list[Node] = []
        for child in node.get("children", []):
            if child.get("type") == "media":
                result.append(child)
            else:
                result.extend(self._media_descendants(child))
        return result

    def _save_and_emit_replace(self, node_id: str, replacement: list[Node]) -> None:
        self._save()
        self.chromeChanged.emit()
        self.nodeReplaced.emit(node_id, [self._qml_node(node) for node in replacement])
        self._emit_section_counts()

    def _remember_deleted_sources(self, node: Node, *, include_media: bool) -> None:
        if node.get("meeting_generated"):
            key = node.get("meeting_source_key")
            if key and (include_media or node.get("type") != "media"):
                self._deleted_source_keys.add(str(key))
        for child in node.get("children", []):
            self._remember_deleted_sources(child, include_media=include_media)

    def _qml_node(self, node: Node) -> Node:
        node_type = node.get("type", "")
        if node_type in ("section", "subsection"):
            hue = int(node.get("color_hue", 215))
            colors = section_colors(hue)
            children = [self._qml_node(child) for child in node.get("children", [])]
            return {
                "id": node.get("id", ""),
                "type": node_type,
                "title": node.get("title", ""),
                "color": colors["accent"],
                "textColor": colors["text"],
                "badgeBg": colors["badge"],
                "collapsed": bool(node.get("collapsed", False)),
                "itemCount": count_media(node.get("children", [])),
                "children": children,
            }
        if node_type == "marker":
            return {
                "id": node.get("id", ""),
                "type": "marker",
                "text": node.get("text", ""),
                "children": [],
            }
        return self._media_qml_node(node)

    def _media_qml_node(self, node: Node) -> Node:
        item_id = node.get("id", "")
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        url = self._url_for_node(node)
        cloud_visible, cloud_active, cloud_progress, cloud_tooltip = self._cloud_state(url)
        is_remote = MediaCacheManager.is_remote(url)
        is_missing = bool(url and not is_remote and not os.path.exists(url))
        return {
            "id": item_id,
            "type": "media",
            "title": node.get("title") or _ref_title(ref) or _tr("_MediaRow", "Media"),
            "mediaType": media_type,
            "badge": self._badge_for(ref, media_type),
            "duration": self._duration_for(node),
            "thumbSource": self._thumb_source_for(item_id),
            "url": url,
            "cloudVisible": cloud_visible,
            "cloudActive": cloud_active,
            "cloudProgress": cloud_progress,
            "cloudTooltip": cloud_tooltip,
            "isMissing": is_missing,
            "children": [],
        }

    def _badge_for(self, _ref: dict[str, Any], media_type: str) -> str:
        if media_type == "image":
            return _tr("PlaylistPanel", "Image")
        if media_type == "audio":
            return _tr("PlaylistPanel", "Audio")
        return _tr("PlaylistPanel", "Video")

    def _media_type_from_ref(self, ref: dict[str, Any]) -> str:
        mime = str(ref.get("mime_type", "")).lower()
        if "image" in mime:
            return "image"
        if "audio" in mime:
            return "audio"
        return "video"

    def _duration_for(self, node: Node) -> str:
        ticks = self._duration_ticks(node)
        return _format_duration(ticks)

    def _duration_ticks(self, node: Node | None) -> int:
        if not node:
            return 0
        try:
            return int(node.get("base_duration_ticks")
                       or (node.get("media_ref") or {}).get("base_duration_ticks")
                       or 0)
        except (TypeError, ValueError):
            return 0

    def _thumbnail_local_path(self, node: Node | None, item_id: str = "") -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        stored = str((node or {}).get("thumbnail_local_path") or "")
        if stored:
            return stored
        return os.fspath(meeting_thumb_path(item_id)) if item_id else ""

    def _has_local_thumbnail(self, node: Node | None) -> bool:
        item_id = str((node or {}).get("id", ""))
        path = self._thumbnail_local_path(node, item_id)
        return bool(path and os.path.exists(path))

    def _save_thumbnail_for_node(self, node: Node, pixmap: QPixmap) -> str:
        item_id = str(node.get("id", ""))
        if not item_id or pixmap is None or pixmap.isNull():
            return ""
        path = meeting_thumb_path(item_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not pixmap.save(os.fspath(path), "JPEG", THUMB_JPEG_QUALITY):
                return ""
        except Exception:  # noqa: BLE001 - Qt image codec boundary
            log_ignored_exception(__name__, "Could not save meeting thumbnail")
            return ""
        node["thumbnail_cache_key"] = meeting_thumb_cache_key(item_id)
        node["thumbnail_local_path"] = os.fspath(path)
        return os.fspath(path)

    def _thumb_source_for(self, item_id: str) -> str:
        node = self._find_node(item_id)
        version = self._thumb_versions.get(item_id, 0)
        thumb_path = self._thumbnail_local_path(node, item_id)
        if version > 0 or (thumb_path and os.path.exists(thumb_path)):
            return f"image://playlistthumbs/{item_id}/{version}"
        return ""

    def _url_for_node_id(self, node_id: str) -> str:
        node = self._find_node(node_id)
        return self._url_for_node(node) if node else ""

    def _url_for_node(self, node: Node | None) -> str:
        if not node:
            return ""
        item_id = node.get("id", "")
        if item_id in self._resolved_urls:
            return self._resolved_urls[item_id]
        if node.get("resolved_url"):
            return str(node.get("resolved_url"))
        ref = node.get("media_ref") or {}
        path = _usable_ref_file_path(ref)
        if path:
            return path
        return ""

    def _cloud_state(self, url: str) -> tuple[bool, bool, float, str]:
        if not MediaCacheManager.is_remote(url):
            return False, False, -1.0, ""
        mgr = MediaCacheManager.instance()
        if mgr.is_cached(url):
            return False, False, 1.0, ""
        active = mgr.is_prefetching(url)
        progress = self._cloud_progress_by_url.get(url, -1.0)
        if active and progress >= 0:
            tooltip = tr_offline_downloading_progress(int(round(progress * 100)))
        elif active:
            tooltip = tr_offline_downloading()
        else:
            tooltip = tr_offline_download()
        return True, active, progress, tooltip

    def _start_media_requests(self, nodes: list[Node] | None = None) -> None:
        for node in iter_nodes(self._nodes if nodes is None else nodes):
            if node.get("type") == "media":
                self._start_media_request(node)

    def _start_media_request(self, node: Node) -> None:
        item_id = node.get("id", "")
        if not item_id:
            return
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        url = self._url_for_node(node)
        has_thumb = self._has_local_thumbnail(node)
        has_duration = media_type == "image" or self._duration_ticks(node) > 0

        if media_type == "image" and url and os.path.exists(url):
            if not has_thumb:
                pix = QPixmap(url)
                if not pix.isNull():
                    self._thumb_cache[item_id] = pix
                    self._save_thumbnail_for_node(node, pix)
                    self._thumb_versions[item_id] = self._thumb_versions.get(item_id, 0) + 1
                    self._save()
                    self._emit_media_changed(item_id)
            return

        if url:
            thumb_url = str(node.get("thumbnail_url") or "")
            if not has_thumb and thumb_url:
                self._queue_info(item_id, thumb_url, "image", purpose="thumb")
            if not has_thumb and not thumb_url:
                self._queue_info(item_id, url, media_type, purpose="metadata")
            elif not has_duration:
                self._queue_info(item_id, url, media_type, purpose="metadata")
            self._emit_cloud_for_node(item_id)
            return

        if ref.get("key_symbol") or ref.get("meps_doc_id"):
            request_id = f"meetingtree:{item_id}:{uuid.uuid4().hex}"
            self._resolve_to_node_id[request_id] = item_id
            self._svc.resolve_video_async(request_id, _meeting_media_from_ref(ref))

    def _queue_info(
        self,
        item_id: str,
        url: str,
        media_type: str,
        *,
        purpose: str = "metadata",
    ) -> None:
        request_key = (item_id, purpose)
        if request_key in self._active_info_requests:
            return
        token = self._next_token
        self._next_token += 1
        self._token_to_node_id[token] = item_id
        self._active_info_requests.add(request_key)
        self._info_queue.request(token, url, media_type)

    @Slot(str, str, str, str)
    def _on_video_resolved(self, request_id: str, url: str, title: str, thumb_url: str):
        item_id = self._resolve_to_node_id.pop(request_id, "")
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node:
            return
        if url:
            if node.get("resolved_url") and node.get("resolved_url") != url:
                node.pop("base_duration_ticks", None)
            node["resolved_url"] = url
            self._resolved_urls[item_id] = url
            if title and self._should_accept_resolved_title(node):
                node["title"] = title
                node["auto_title"] = False
                node.setdefault("media_ref", {})["label"] = title
        if thumb_url:
            if node.get("thumbnail_url") and node.get("thumbnail_url") != thumb_url:
                node.pop("thumbnail_local_path", None)
                node.pop("thumbnail_cache_key", None)
            node["thumbnail_url"] = thumb_url
        target = thumb_url or url
        if target:
            media_type = "image" if thumb_url else (node.get("media_type") or "video")
            purpose = "thumb" if thumb_url else "metadata"
            self._queue_info(item_id, target, media_type, purpose=purpose)
        if url and not self._duration_ticks(node) and target != url:
            self._queue_info(item_id, url, node.get("media_type") or "video", purpose="metadata")
        self._save()
        self._emit_media_changed(item_id)
        self._emit_cloud_for_node(item_id)

    @Slot(int, object, str)
    def _on_info_ready(self, token: int, pixmap: QPixmap, title: str):
        item_id = self._token_to_node_id.get(token, "")
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node:
            return
        if pixmap and not pixmap.isNull():
            self._thumb_cache[item_id] = pixmap
            self._save_thumbnail_for_node(node, pixmap)
            self._thumb_versions[item_id] = self._thumb_versions.get(item_id, 0) + 1
        if title and self._should_accept_resolved_title(node):
            node["title"] = title
            node["auto_title"] = False
            node.setdefault("media_ref", {})["label"] = title
        self._save()
        self._emit_media_changed(item_id)

    def _should_accept_resolved_title(self, node: Node) -> bool:
        if node.get("user_title_override"):
            return False
        title = str(node.get("title", "") or "").strip()
        ref = node.get("media_ref") or {}
        placeholder_titles = {
            "",
            "Media",
            _tr("_MediaRow", "Media"),
        }
        return (
            bool(node.get("auto_title"))
            or (not _ref_title(ref) and title in placeholder_titles)
            or is_filename_title(title)
        )

    @Slot(int, int)
    def _on_duration_ready(self, token: int, duration_ms: int):
        item_id = self._token_to_node_id.get(token, "")
        if not item_id or duration_ms <= 0:
            return
        node = self._find_node(item_id)
        if not node:
            return
        ticks = int(duration_ms) * 10_000
        if node.get("base_duration_ticks") == ticks:
            return
        node["base_duration_ticks"] = ticks
        self._save()
        self._emit_media_changed(item_id)

    @Slot(str)
    def _on_cache_changed(self, url: str):
        self._emit_cloud_for_url(url)

    @Slot(str)
    def _on_cache_removed(self, path: str):
        from ...core.media.cache import cached_path_for
        removed = os.path.normcase(os.path.abspath(path))
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media":
                continue
            url = self._url_for_node(node)
            if not MediaCacheManager.is_remote(url):
                continue
            if os.path.normcase(os.path.abspath(cached_path_for(url))) == removed:
                self._emit_cloud_for_node(str(node.get("id", "")))

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if total > 0:
            self._cloud_progress_by_url[url] = downloaded / total
        self._emit_cloud_for_url(url)

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str):
        self._cloud_progress_by_url.pop(url, None)
        self._emit_cloud_for_url(url)

    def _emit_media_changed(self, item_id: str) -> None:
        node = self._find_node(item_id)
        if not node:
            return
        self.mediaChanged.emit(
            item_id,
            str(node.get("title", "")),
            self._duration_for(node),
            self._thumb_source_for(item_id),
        )

    def _emit_cloud_for_url(self, url: str) -> None:
        if not url:
            return
        for node in iter_nodes(self._nodes):
            if node.get("type") == "media" and self._url_for_node(node) == url:
                self._emit_cloud_for_node(str(node.get("id", "")))

    def _emit_cloud_for_node(self, item_id: str) -> None:
        node = self._find_node(item_id)
        if not node:
            return
        visible, active, progress, tooltip = self._cloud_state(self._url_for_node(node))
        self.cloudChanged.emit(item_id, visible, active, progress, tooltip)

    def _emit_section_counts(self) -> None:
        counts = {
            str(node.get("id", "")): count_media(node.get("children", []))
            for node in iter_nodes(self._nodes)
            if node.get("type") in ("section", "subsection")
        }
        self.sectionCountsChanged.emit(counts)
        self.chromeChanged.emit()

    def _to_int(self, value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
