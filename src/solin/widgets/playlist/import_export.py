from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
import copy
import os
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QTimer

from ...core.i18n.strings import tr_document_page_title
from ...core.jw.language_context import jw_media_language_context
from ...core.playlists.items import create_playlist_item, distinct_playlist_item_ids
from ...core.playlists.tree_editing import build_playlist_tree, insert_playlist_media
from ...core.meetings.tree_editing import children_for_tree_target, parse_tree_list_id
from ...core.foundation.resource_keys import child_folder_resource_claim
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.ingest.watched_folder_files import WatchedFolderCopyRequest, WatchedFolderCopyResult
from ...core.media.identity import PLAYLIST_MEDIA_OCCURRENCES, partition_media_items
from ...core.media.operations import (
    MediaOperationCancelled, MediaOperationPresentation, MediaOperationProgress,
    MediaOperationSpec, MediaOperationState,
)
from ...core.media.placement import END_OF_LIST_INDEX


@dataclass(frozen=True, slots=True)
class _PreparedJwpubPlaylistBatch:
    items: list[dict]
    copies: tuple[WatchedFolderCopyResult, ...]


__all__ = ("PlaylistEditImportMixin",)


class PlaylistEditImportMixin:
    def _import_pdfs(
        self,
        pdf_paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        for pdf_path in pdf_paths:
            self._import_single_pdf(pdf_path, insert_at=insert_at, section_id=section_id)

    def _import_single_pdf(
        self,
        pdf_path: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        stem = Path(pdf_path).stem
        pages = self._document_conversion_service.cached_pdf_pages(pdf_path)
        if pages:
            self._on_pdf_pages_ready(pages, stem, insert_at, section_id)
            return
        self._notifications.information(
            self.tr("Converting PDF: {name}…").replace("{name}", str(stem))
        )
        thread = self._document_conversion_service.create_pdf_thread(
            pdf_path,
            parent=self,
        )
        self._pdf_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, stem, _pos=insert_at, _sid=section_id:
                self._on_pdf_pages_ready(pages, stem, _pos, _sid)
        )
        thread.conversion_failed.connect(self._on_pdf_failed)
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t)
            if t in self._pdf_threads else None
        )
        thread.start()

    def _on_pdf_pages_ready(
        self,
        pages: list[str],
        pdf_stem: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        new_items = [
            create_playlist_item(
                title=tr_document_page_title(pdf_stem, i + 1),
                url=page_path,
                type="image",
                section_id=section_id,
            )
            for i, page_path in enumerate(pages)
        ]
        items = self._pl.setdefault("items", [])
        if insert_at < 0 or insert_at >= len(items):
            items.extend(new_items)
        else:
            for i, ni in enumerate(new_items):
                items.insert(insert_at + i, ni)
        self._save()
        self._reconcile_playlist()
        self._notifications.success(
            self.tr("{name}: %n page(s) opened", "", len(new_items)).format(
                name=pdf_stem
            )
        )

    def _on_pdf_failed(self, error_msg: str) -> None:
        self._notifications.error(
            self.tr("Error converting PDF: {error}").replace("{error}", str(error_msg))
        )

    def _import_jwpubs(
        self,
        paths: list[str],
        *,
        list_id: str | None = None,
        insert_index: int = END_OF_LIST_INDEX,
        public_talk: bool = False,
    ) -> None:
        playlist = self._pl
        session = self._tree_session
        if playlist is None or not session.owner_id:
            return
        owner_id, generation = session.owner_id, session.generation
        origin_generation = self._jwpub_origin_generation
        language = jw_media_language_context(self.lang).api_code
        folder_path = self._watched_path
        playlist_id = str(playlist.get("id") or "")
        inserted_count = 0

        def is_current() -> bool:
            return (
                self._jwpub_origin_generation == origin_generation
                and self._tree_session is session
                and session.owner_id == owner_id
                and session.generation == generation
                and self._pl is playlist
                and str(playlist.get("id") or "") == playlist_id
                and self._watched_path == folder_path
                and jw_media_language_context(self.lang).api_code == language
            )

        def insert(items, target_list_id, target_index, completed) -> None:
            nonlocal inserted_count
            acknowledged = False
            batch: list[dict] = []

            def finish(success: bool) -> None:
                nonlocal acknowledged, inserted_count
                if not acknowledged:
                    acknowledged = True
                    if success:
                        inserted_count += len(batch)
                    completed(success)

            if not is_current():
                finish(False)
                return
            tree = build_playlist_tree(playlist)
            if children_for_tree_target(tree, *parse_tree_list_id(target_list_id)) is None:
                finish(False)
                return
            partition = partition_media_items(
                playlist.get("items", []), items,
                occurrence_policy=PLAYLIST_MEDIA_OCCURRENCES,
            )
            batch = distinct_playlist_item_ids(playlist.get("items", []), partition.unique_items)
            if (
                list_id is not None and target_list_id == list_id
                and target_index == insert_index and target_index < END_OF_LIST_INDEX
            ):
                target_index += inserted_count
            if not batch:
                finish(True)
                return
            previous_playlist = None
            try:
                if self._is_watched and folder_path:
                    self._queue_jwpub_playlist_batch(
                        batch, target_list_id, target_index,
                        is_current=is_current, completed=finish,
                    )
                    return
                previous_playlist = copy.deepcopy(playlist)
                inserted = session.insert_media(target_list_id, target_index, batch)
                if inserted:
                    self._save()
                    self._reconcile_playlist()
                finish(inserted)
            except Exception:  # noqa: BLE001 - complete the selector's origin insertion boundary
                log_ignored_exception(__name__, "Could not insert selected publication media")
                try:
                    if previous_playlist is not None:
                        playlist.clear()
                        playlist.update(previous_playlist)
                        session.refresh()
                finally:
                    finish(False)

        self._jwpub_selection_factory.select(
            paths,
            parent=self,
            operations=self._media_tree_runtime.operations,
            language=language,
            dest_images_dir=os.fspath(self._profile_paths.images_dir),
            scope_id=owner_id,
            is_current=is_current,
            placement_ref=lambda: copy.deepcopy(playlist) if is_current() else None,
            insert=insert,
            list_id=list_id,
            insert_index=insert_index,
            public_talk=public_talk,
        )

    def _import_public_talk_media(self) -> None:
        self._import_jwpubs([], public_talk=True)

    def _queue_jwpub_playlist_batch(
        self,
        items: list[dict],
        list_id: str,
        insert_index: int,
        *,
        is_current: Callable[[], bool],
        completed: Callable[[bool], None],
    ) -> None:
        """Copy one ordered batch before committing it to the linked playlist."""
        playlist = self._pl
        folder_path = self._watched_path
        session = self._tree_session
        operation_id = f"linked-jwpub-copy:{uuid4().hex}"
        stage = self.tr("Preparing media")
        prepared_result: list[_PreparedJwpubPlaylistBatch] = []
        finished = False
        rollback_started = False
        self._jwpub_copy_operations.add(operation_id)
        if self._wf_refresh_inflight is not None:
            self._wf_refresh_superseded = True
            self._wf_refresh_pending = True
        for offset, item in enumerate(items):
            session.add_pending(
                item, target_list_id=list_id,
                insert_index=insert_index + offset if insert_index < END_OF_LIST_INDEX else insert_index,
            )

        def run(report, cancellation):
            prepared = copy.deepcopy(items)
            copies: dict[str, WatchedFolderCopyResult] = {}
            try:
                for offset, item in enumerate(prepared):
                    if cancellation.is_set():
                        raise MediaOperationCancelled("Publication copy cancelled")
                    url = str(item.get("url") or "")
                    if url and not url.startswith(("http://", "https://")):
                        key = os.path.normcase(os.path.abspath(url))
                        result = copies.get(key)
                        if result is None:
                            result = self._watched_folder_file_store.copy_file_transaction(
                                WatchedFolderCopyRequest(
                                    source=Path(url), folder=Path(folder_path),
                                    operation_id=f"{operation_id}-{offset}",
                                ),
                                cancellation=cancellation,
                            )
                            copies[key] = result
                        item["url"] = os.fspath(result.destination)
                    report(MediaOperationProgress(
                        state=MediaOperationState.COPYING, stage=stage,
                        completed=offset + 1, total=len(prepared),
                    ))
                if cancellation.is_set():
                    raise MediaOperationCancelled("Publication copy cancelled")
                value = _PreparedJwpubPlaylistBatch(prepared, tuple(copies.values()))
                prepared_result.append(value)
                return value
            except BaseException:  # noqa: BLE001 - roll back owned copies before worker propagation
                self._watched_folder_file_store.rollback_copies(folder_path, copies.values())
                raise

        def finish(success):
            nonlocal finished
            if finished:
                return
            finished = True
            for item in items:
                session.remove_pending(str(item["id"]))
            self._jwpub_copy_operations.discard(operation_id)
            if is_current():
                session.refresh()
                self._sync_playlist_chrome(emit_data_changed=False)
                if self._wf_refresh_pending:
                    QTimer.singleShot(0, self.refresh_watched_folder)
            completed(success)

        def discard(value=None):
            nonlocal rollback_started
            if rollback_started or finished:
                return
            rollback_started = True
            if value is None and prepared_result:
                value = prepared_result.pop()
            else:
                prepared_result.clear()
            if isinstance(value, _PreparedJwpubPlaylistBatch) and value.copies:
                copies = value.copies

                def rollback(_report, _cancellation):
                    self._watched_folder_file_store.rollback_copies(folder_path, copies)

                accepted = self._media_tree_runtime.operations.submit(MediaOperationSpec(
                    operation_id=f"linked-jwpub-rollback:{uuid4().hex}",
                    scope_id=operation_id,
                    operation_type="linked_folder_rollback",
                    conflict_key=child_folder_resource_claim(folder_path),
                    presentation=MediaOperationPresentation.BACKGROUND,
                    runner=rollback, commit=lambda _: finish(False),
                    failed=lambda *_: finish(False), cancelled=lambda: finish(False),
                ))
                if accepted:
                    return
                self._media_tree_runtime.snapshots.request(
                    f"linked-jwpub-rollback:{uuid4().hex}",
                    lambda: rollback(None, None),
                    conflict_key=child_folder_resource_claim(folder_path),
                )
            finish(False)

        def commit(value):
            if not isinstance(value, _PreparedJwpubPlaylistBatch):
                raise TypeError("Publication copy returned an invalid result")
            previous_playlist = copy.deepcopy(playlist)
            if not is_current() or not insert_playlist_media(
                playlist, list_id, insert_index, value.items,
            ):
                discard(value)
                return
            try:
                self._schedule_manifest_save(folder_path, playlist)
            except Exception:  # noqa: BLE001 - restore origin state before copy rollback
                playlist.clear()
                playlist.update(previous_playlist)
                raise
            prepared_result.clear()
            finish(True)

        submitted = self._media_tree_runtime.operations.submit(MediaOperationSpec(
            operation_id=operation_id, scope_id=session.owner_id,
            operation_type="linked_folder_copy",
            conflict_key=child_folder_resource_claim(folder_path),
            presentation=MediaOperationPresentation.TREE_LOCAL,
            runner=run, commit=commit, initial_stage=stage, priority=100,
            failed=lambda *_args: discard(), cancelled=discard, discarded=discard,
        ))
        if not submitted:
            discard()

    def _import_lo_files(
        self,
        paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        for path in paths:
            self._import_single_lo(path, insert_at=insert_at, section_id=section_id)

    def _import_single_lo(
        self,
        lo_path: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        stem = Path(lo_path).stem
        pages = self._document_conversion_service.cached_office_pages(lo_path)
        if pages:
            self._on_lo_pages_ready(pages, stem, insert_at, section_id)
            return
        self._notifications.information(
            self.tr("Converting PDF: {name}…").replace("{name}", str(stem))
        )
        thread = self._document_conversion_service.create_office_thread(
            lo_path,
            parent=self,
        )
        self._lo_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, stem, _pos=insert_at, _sid=section_id:
                self._on_lo_pages_ready(pages, stem, _pos, _sid)
        )
        thread.conversion_failed.connect(self._on_pdf_failed)
        thread.finished.connect(
            lambda t=thread: self._lo_threads.remove(t)
            if t in self._lo_threads else None
        )
        thread.start()

    def _on_lo_pages_ready(
        self,
        pages: list[str],
        stem: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        new_items = [
            create_playlist_item(
                title=tr_document_page_title(stem, i + 1),
                url=page_path,
                type="image",
                section_id=section_id,
            )
            for i, page_path in enumerate(pages)
        ]
        items = self._pl.setdefault("items", [])
        if insert_at < 0 or insert_at >= len(items):
            items.extend(new_items)
        else:
            for i, ni in enumerate(new_items):
                items.insert(insert_at + i, ni)
        self._save()
        self._reconcile_playlist()
        self._notifications.success(
            self.tr("{name}: %n page(s) opened", "", len(new_items)).format(
                name=stem
            )
        )

    def _import_jwlplaylists_drop(
        self,
        jwl_paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        self.import_jwl_requested.emit(jwl_paths, insert_at, section_id)

    def commit_imported_jwl_items(
        self,
        items: list[dict],
        *,
        insert_at: int,
        expected_playlist_id: str,
    ) -> None:
        if not self._pl or str(self._pl.get("id")) != expected_playlist_id:
            raise RuntimeError(self.tr("The destination playlist is no longer open."))
        if not items:
            return
        target = self._pl.setdefault("items", [])
        previous_items = list(target)
        if insert_at < 0 or insert_at >= len(target):
            target.extend(items)
        else:
            target[insert_at:insert_at] = items
        try:
            if self._is_temp:
                pass
            elif self._is_watched:
                self._save()
            else:
                self._playlist_repository.save_strict(self._all_playlists)
        except Exception:  # noqa: BLE001 - transactional playlist commit rollback
            self._pl["items"] = previous_items
            self._reconcile_playlist()
            raise
        self._reconcile_playlist()
        self._notifications.success(
            self.tr("%n item(s) imported", "", len(items))
        )
