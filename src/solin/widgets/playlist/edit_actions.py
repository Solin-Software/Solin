from __future__ import annotations

import copy
import os
import random
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QFileDialog

from ...core.foundation.constants import (
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
)
from ...core.media.formats import MEDIA_EXTS, media_type_from_path
from ...core.media.destinations import create_media_destination_request
from ...core.media.identity import PLAYLIST_MEDIA_OCCURRENCES, partition_media_items
from ...core.media.insertion import MediaInsertPayload, MediaInsertResult
from ...core.media.operations import (
    MediaOperationPresentation,
    MediaOperationProgress,
    MediaOperationSpec,
    MediaOperationState,
)
from ...core.projection.idle_media import (
    create_idle_media_request,
    existing_idle_media_path,
)
from ...core.media.thumbnail_identity import (
    thumbnail_source_fingerprint,
    thumbnail_storage_id,
)
from ...core.foundation.resource_keys import child_folder_resource_claim
from ...core.ingest.watched_folder_files import (
    WatchedFolderCopyRequest,
    WatchedFolderCopyResult,
)
from ...core.playlists.tree_editing import insert_playlist_media
from ...core.playlists.items import (
    copy_playlist_item_for_destination,
    create_playlist_item,
    create_playlist_item_from_insert,
    distinct_playlist_item_ids,
)
from ...ui.qml.media_tree.state import MediaAvailability
from .dialogs import NameDialog


__all__ = ("PlaylistEditActionsMixin",)


class PlaylistEditActionsMixin:
    def _list_id_for_section(self, section_id: str) -> str:
        if not section_id or not self._pl:
            return "root"
        section = next(
            (s for s in self._pl.get("sections", []) if s.get("id") == section_id),
            None,
        )
        if not section:
            return "root"
        prefix = "subsection" if section.get("parent_id") else "section"
        return f"{prefix}:{section_id}"

    def _sync_playlist_chrome(self, *, emit_data_changed: bool = True) -> None:
        if not self._pl:
            return
        items = self._pl.get("items", [])
        n = len(items)
        self.bridge.set_state(
            name=self._pl.get("name", ""),
            is_temp=self._is_temp,
            is_watched=self._is_watched,
            is_loading=False,
            item_count=n,
            has_entries=bool(
                items
                or self._pl.get("sections")
                or self._pl.get("markers")
                or self._tree_session.pending_items()
            ),
            emit_data_changed=emit_data_changed,
        )

    def _add_files(
        self,
        paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
        target_list_id: str = "",
        target_list_index: int = -1,
    ) -> None:
        if not self._pl:
            return
        added = skipped = 0
        new_items: list[dict[str, Any]] = []
        pending_items = list(self._tree_session.pending_items())
        for path in paths:
            if Path(path).suffix.lower() not in MEDIA_EXTS:
                continue

            if self._is_watched and self._watched_path:
                candidate = dict(
                    create_playlist_item(
                        title=Path(path).stem,
                        url=path,
                        **({"section_id": section_id} if section_id else {}),
                    )
                )
                if partition_media_items(
                    [*self._pl.get("items", []), *pending_items],
                    [candidate],
                    occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
                ).duplicate_items:
                    skipped += 1
                    continue
                list_id = target_list_id or self._list_id_for_section(section_id)
                list_index = target_list_index if target_list_index >= 0 else -1
                self._queue_watched_media_copy(
                    candidate,
                    source_path=path,
                    target_list_id=list_id,
                    target_list_index=list_index,
                )
                pending_items.append(candidate)
                added += 1
                continue

            candidate = dict(
                create_playlist_item(
                    title=Path(path).stem,
                    url=path,
                    **({"section_id": section_id} if section_id else {}),
                )
            )
            if partition_media_items(
                [*self._pl.get("items", []), *new_items],
                [candidate],
                occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
            ).duplicate_items:
                skipped += 1
                continue
            new_items.append(candidate)
            added += 1
        if added and self._is_watched:
            self._sync_playlist_chrome(emit_data_changed=False)
            return
        if added:
            items = self._pl.setdefault("items", [])
            inserted_with_tree = (
                bool(target_list_id)
                and target_list_index >= 0
                and self._tree_session.insert_media(
                    target_list_id,
                    target_list_index,
                    new_items,
                )
            )
            appended_flat = False
            if inserted_with_tree:
                pass
            elif insert_at < 0 or insert_at >= len(items):
                items.extend(new_items)
                appended_flat = True
            else:
                for i, ni in enumerate(new_items):
                    items.insert(insert_at + i, ni)
                self._tree_session.refresh()
            self._save()
            if appended_flat:
                self._tree_session.refresh()
            self._sync_playlist_chrome(emit_data_changed=False)
            QTimer.singleShot(0, self._request_missing_thumbnails)
        if added:
            msg = self.tr("%n file(s) added", "", added)
            if skipped:
                msg = self.tr("{added} · {skipped}").format(
                    added=msg,
                    skipped=self.tr("%n duplicate(s) skipped", "", skipped),
                )
            self._notifications.success(msg)
        elif skipped:
            self._notifications.warning(
                self.tr("%n file(s) already in playlist", "", skipped)
            )

    def _queue_watched_media_copy(
        self,
        item: dict[str, Any],
        *,
        source_path: str,
        target_list_id: str,
        target_list_index: int,
    ) -> None:
        playlist = self._pl
        folder_path = self._watched_path
        owner_id = self._tree_session.owner_id
        session_generation = self._tree_session.generation
        if playlist is None or not folder_path or not owner_id:
            return
        initial_item_ids = {str(current.get("id") or "") for current in playlist.get("items", [])}
        item_id = str(item["id"])
        operation_id = f"linked-copy:{uuid.uuid4().hex}"
        stage = self.tr("Preparing media")
        self._tree_session.add_pending(
            item,
            target_list_id=target_list_id,
            insert_index=target_list_index,
        )
        request = WatchedFolderCopyRequest(
            source=Path(source_path),
            folder=Path(folder_path),
            operation_id=operation_id,
        )

        def run(report, cancellation):
            return self._watched_folder_file_store.copy_file_transaction(
                request,
                progress=lambda completed, total: report(
                    MediaOperationProgress(
                        state=MediaOperationState.COPYING,
                        stage=stage,
                        detail=Path(source_path).name,
                        completed=completed,
                        total=total,
                    )
                ),
                cancellation=cancellation,
            )

        def commit(value: object) -> None:
            if not isinstance(value, WatchedFolderCopyResult):
                raise TypeError("Linked-folder copy returned an invalid result")
            active_playlist = self._tree_session.playlist
            if (
                self._tree_session.owner_id != owner_id
                or self._tree_session.generation != session_generation
                or active_playlist is None
            ):
                discard(value)
                return
            item["url"] = str(value.destination)
            item["title"] = value.destination.stem
            if not value.already_present:
                destination_key = os.path.normcase(
                    os.path.normpath(os.path.abspath(value.destination))
                )
                auto_adopted_ids = {
                    str(current.get("id") or "")
                    for current in active_playlist.get("items", [])
                    if str(current.get("id") or "") not in initial_item_ids
                    and not str(current.get("url") or "").startswith(("http://", "https://"))
                    and os.path.normcase(
                        os.path.normpath(os.path.abspath(str(current.get("url") or "")))
                    )
                    == destination_key
                }
                if auto_adopted_ids:
                    active_playlist["items"] = [
                        current
                        for current in active_playlist.get("items", [])
                        if str(current.get("id") or "") not in auto_adopted_ids
                    ]
            if partition_media_items(
                active_playlist.get("items", []),
                [item],
                occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
            ).duplicate_items:
                self._tree_session.remove_pending(item_id)
                self._tree_session.refresh()
                self._sync_playlist_chrome(emit_data_changed=False)
                self._notifications.warning(self.tr("File already in playlist"))
                return
            inserted = insert_playlist_media(
                active_playlist,
                target_list_id,
                target_list_index,
                [item],
            )
            if not inserted:
                self._tree_session.remove_pending(item_id)
                self._tree_session.refresh()
                discard(value)
                self._sync_playlist_chrome(emit_data_changed=False)
                self._notifications.warning(self.tr("Could not update the linked folder."))
                return
            self._schedule_manifest_save(folder_path, active_playlist)
            if self._tree_session.owner_id == owner_id:
                self._tree_session.remove_pending(item_id)
                self._tree_session.refresh()
                self._sync_playlist_chrome(emit_data_changed=False)
                QTimer.singleShot(0, self._request_missing_thumbnails)
                self._notifications.success(self.tr("1 file added"))

        def discard(value: object | None) -> None:
            if not isinstance(value, WatchedFolderCopyResult) or value.already_present:
                return
            self._media_tree_runtime.schedule_artifact_cleanup(
                (value.destination,),
                conflict_key=child_folder_resource_claim(folder_path),
            )

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=owner_id,
                subject_id=item_id,
                operation_type="linked_folder_copy",
                conflict_key=child_folder_resource_claim(folder_path),
                presentation=MediaOperationPresentation.TREE_LOCAL,
                runner=run,
                commit=commit,
                priority=100,
                initial_stage=stage,
                retryable=True,
                discarded=discard,
            )
        )
        if not submitted:
            self._tree_session.remove_pending(item_id)

    def _on_jw_media_confirmed(
        self,
        item_data: MediaInsertPayload | dict[str, Any],
        target_list_id: str,
        target_index: int,
    ) -> MediaInsertResult:
        if not self._pl:
            return MediaInsertResult(target_valid=False)

        payload = (
            item_data
            if isinstance(item_data, MediaInsertPayload)
            else MediaInsertPayload.from_mapping(item_data)
        )
        pl_item_id = str(uuid.uuid4())
        pl_item = create_playlist_item_from_insert(payload, item_id=pl_item_id)

        partition = partition_media_items(
            self._pl.get("items", []),
            [pl_item],
            occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
        )
        if partition.duplicate_items:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)

        items = self._pl.setdefault("items", [])
        inserted = False
        if target_list_id and target_index >= 0:
            inserted = self._tree_session.insert_media(
                target_list_id,
                target_index,
                [pl_item],
            )

        if not inserted:
            if target_list_id and target_index >= 0:
                return MediaInsertResult(target_valid=False)
            items.append(pl_item)
            self._tree_session.refresh()

        if payload.thumbnail_path:
            self._media_tree_runtime.thumbnails.copy_file(
                owner_id=self._tree_session.owner_id,
                node_id=pl_item_id,
                storage_id=thumbnail_storage_id(
                    pl_item_id,
                    str(pl_item.get("url") or ""),
                ),
                store=self._playlist_thumbnail_store,
                source=payload.thumbnail_path,
                source_signature=(
                    thumbnail_source_fingerprint(payload.thumbnail_url)
                    if payload.thumbnail_url
                    else ""
                ),
            )

        self._save()
        self._sync_playlist_chrome(emit_data_changed=False)

        QTimer.singleShot(0, self._request_missing_thumbnails)
        return MediaInsertResult(added_items=(pl_item,))

    def _add_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("Add Media"),
            os.path.expanduser("~"),
            "Media, PDF & Publication "
            "(*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.wav "
            "*.jpg *.jpeg *.png *.gif *.webp *.pdf *.jwpub *.jwlplaylist);;"
            "JW Playlist (*.jwlplaylist);;"
            "JW Publication (*.jwpub);;"
            "PDF (*.pdf);;"
            "Video (*.mp4 *.mkv *.mov *.avi *.webm);;"
            "Audio (*.mp3 *.m4a *.wav);;"
            "Image (*.jpg *.jpeg *.png *.gif *.webp);;"
            "All (*)",
        )
        if not paths:
            return

        media = [p for p in paths if Path(p).suffix.lower() in MEDIA_EXTS]
        pdfs = [p for p in paths if Path(p).suffix.lower() in PDF_EXTS]
        jwpubs = [p for p in paths if Path(p).suffix.lower() in JWPUB_EXTS]
        jwl_files = [p for p in paths if Path(p).suffix.lower() in PLAYLIST_EXTS]
        if media:
            self._add_files(media, insert_at=-1)
        if pdfs:
            self._import_pdfs(pdfs)
        if jwpubs:
            self._import_jwpubs(jwpubs)
        if jwl_files:
            self._import_jwlplaylists_drop(jwl_files)

    def _project_by_id(self, item_id: str) -> None:
        self._flush_image_framing_save()
        if not self._pl:
            return
        items = self._pl.get("items", [])
        try:
            idx = next(i for i, it in enumerate(items) if it["id"] == item_id)
        except StopIteration:
            return
        self.project_items.emit(items[idx:] + items[:idx], 0, "")

    def _add_to_destination(self, item_id: str) -> None:
        self._flush_image_framing_save()
        if not self._pl:
            return
        item = next(
            (
                candidate
                for candidate in self._pl.get("items", [])
                if candidate.get("id") == item_id
            ),
            None,
        )
        if item is None:
            return
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        if state.availability != MediaAvailability.AVAILABLE:
            return
        destination_item = copy_playlist_item_for_destination(item)
        request = create_media_destination_request(destination_item)
        if request is None:
            return
        self.media_destination_requested.emit(request)

    def _set_as_idle(self, item_id: str) -> None:
        self._flush_image_framing_save()
        if not self._pl:
            return
        item = next(
            (
                candidate
                for candidate in self._pl.get("items", [])
                if candidate.get("id") == item_id
            ),
            None,
        )
        if item is None:
            return
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        if state.availability != MediaAvailability.AVAILABLE:
            return
        media_type = str(item.get("type") or "video").strip().lower()
        source = existing_idle_media_path(media_type, str(item.get("url") or ""))
        if not source:
            return
        thumbnail_path = ""
        if media_type == "video":
            thumbnail_path = os.fspath(
                self._playlist_thumbnail_store.path(
                    thumbnail_storage_id(item_id, source)
                )
            )
        request = create_idle_media_request(
            title=str(item.get("title") or ""),
            media_type=media_type,
            source=source,
            thumbnail_path=thumbnail_path,
        )
        if request is None:
            return
        self.set_as_idle_requested.emit(request)

    def _is_playable(self, item: dict) -> bool:
        url = item.get("url", "")
        if not url:
            return False
        if url.startswith(("http://", "https://")):
            return True
        item_id = str(item.get("id") or "")
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        return state.availability not in {
            MediaAvailability.MISSING,
            MediaAvailability.ERROR,
        }

    def _do_play_all(self) -> None:
        self._flush_image_framing_save()
        if not self._pl:
            return
        items = [it for it in self._pl.get("items", []) if self._is_playable(it)]
        if items:
            self.project_items.emit(items, 0, "next")

    def _do_shuffle(self) -> None:
        self._flush_image_framing_save()
        if not self._pl:
            return
        items = [it for it in self._pl.get("items", []) if self._is_playable(it)]
        if not items:
            return
        s = random.randrange(len(items))
        self.project_items.emit(items[s:] + items[:s], 0, "random")

    def _export(self, playlist_format: str) -> None:
        if not self._pl:
            return
        self.export_requested.emit(playlist_format)

    def _on_back(self) -> None:
        self._is_temp = False
        self._sync_order()
        self.back_requested.emit()

    def load_temp_playlist(
        self,
        items: list,
        lang=None,
        name: str | None = None,
        *,
        playlist_id: str | None = None,
    ) -> str:
        if lang:
            self.lang = lang
        self._is_temp = True
        display_name = name or self.tr("Current playback")
        norm = []
        for raw in items:
            item = dict(raw)
            if not item.get("id"):
                item["id"] = str(uuid.uuid4())
            if not item.get("type"):
                item["type"] = media_type_from_path(
                    item.get("url", ""),
                    default="video",
                )
            norm.append(item)
        temp_id = playlist_id or f"__temp__:{uuid.uuid4()}"
        pl = {
            "id": temp_id,
            "name": display_name,
            "items": distinct_playlist_item_ids([], norm),
            "_temp": True,
        }
        self.load_playlist(pl)
        return temp_id

    def _save_temp_playlist(self) -> None:
        dlg = NameDialog(lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Save playlist"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        self.save_temp_as_permanent.emit(name, copy.deepcopy(self._pl or {}))
