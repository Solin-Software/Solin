"""Application workflow for native and JW Library playlist transfers."""

from __future__ import annotations

import copy
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QFileDialog

from solin.controllers.playlist_transfer_controller import (
    PlaylistTransferController,
    PlaylistTransferJob,
    PlaylistTransferProgress,
)
from solin.core.foundation.constants import VERSION
from solin.core.jw.language_context import jw_media_language_context
from solin.core.playlists.jwl_export import (
    JwlPlaylistExportRequest,
    export_jwlplaylist_document,
)
from solin.core.playlists.jwl_files import read_jwlplaylist_document
from solin.core.playlists.jwl_import import playlist_items_from_jwl_document_items
from solin.core.playlists.native import (
    NativePlaylistExportRequest,
    NativePlaylistImportRequest,
    NativePlaylistImportResult,
    cleanup_stale_native_playlist_imports,
    export_native_playlist,
    import_native_playlist,
    rollback_native_playlist_import,
)
from solin.core.playlists.names import PlaylistNameRegistry


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True, slots=True)
class _JwlImportResult:
    playlists: tuple[dict, ...]
    created_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _JwlItemsImportResult:
    items: tuple[dict, ...]
    created_files: tuple[Path, ...]


class PlaylistTransferWorkflow(QObject):
    """Own file pickers, worker jobs, persistence, and rollback policies."""

    def __init__(
        self,
        *,
        parent,
        notifications,
        language_manager,
        playlists: list[dict],
        playlist_repository,
        profile_paths,
        profile_media_store,
        media_cache_manager,
        refresh_playlists: Callable[[], None],
        open_playlist: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self._parent_widget = parent
        self._notifications = notifications
        self._language_manager = language_manager
        self._playlists = playlists
        self._playlist_repository = playlist_repository
        self._profile_paths = profile_paths
        self._profile_media_store = profile_media_store
        self._media_cache_manager = media_cache_manager
        self._refresh_playlists = refresh_playlists
        self._open_playlist = open_playlist
        self._coordinator = PlaylistTransferController(
            parent=parent,
            notifications=notifications,
        )
        cleanup_stale_native_playlist_imports(
            self._profile_paths.embedded_dir,
            self._profile_paths.thumb_cache_dir,
        )

    @property
    def is_busy(self) -> bool:
        return self._coordinator.is_busy

    def shutdown(self) -> None:
        self._coordinator.shutdown()

    def export_playlist(self, playlist: dict, playlist_format: str) -> None:
        snapshot = copy.deepcopy(playlist)
        name = str(snapshot.get("name") or self.tr("Playlist"))
        output_path = self._choose_export_path(name, playlist_format)
        if not output_path:
            return

        if playlist_format == "solin":
            runner = self._native_export_runner(snapshot, output_path)
            title = self.tr("Export Solin Playlist")
        elif playlist_format == "jwl":
            runner = self._jwl_export_runner(snapshot, output_path)
            title = self.tr("Export JW Library Playlist")
        else:
            raise ValueError(f"Unsupported playlist format: {playlist_format}")

        self._submit(
            PlaylistTransferJob(
                key=f"export:{playlist_format}:{output_path}",
                title=title,
                initial_stage=self.tr("Preparing playlist…"),
                runner=runner,
                completed=lambda _result: None,
                succeeded_message=self.tr("Exported: {path}").replace(
                    "{path}", output_path
                ),
            )
        )

    def import_playlists(
        self,
        paths: list[str],
        playlist_format: str,
        *,
        open_after: bool = False,
    ) -> None:
        normalized = [os.fspath(Path(path)) for path in paths]
        if not normalized:
            return
        if playlist_format == "solin":
            job = self._native_import_job(normalized, open_after=open_after)
        elif playlist_format == "jwl":
            job = self._jwl_import_job(normalized, open_after=open_after)
        else:
            raise ValueError(f"Unsupported playlist format: {playlist_format}")
        self._submit(job)

    def import_jwl_items(
        self,
        paths: list[str],
        *,
        section_id: str,
        completed: Callable[[list[dict]], None],
    ) -> None:
        normalized = [os.fspath(Path(path)) for path in paths]
        if not normalized:
            return

        def commit(result: object) -> None:
            imported = result
            if not isinstance(imported, _JwlItemsImportResult):
                raise TypeError("Invalid JWL item import result")
            try:
                completed(list(imported.items))
            except Exception:  # noqa: BLE001 - imported-media rollback boundary
                self._rollback_profile_files(imported.created_files)
                raise

        self._submit(
            PlaylistTransferJob(
                key=f"jwl-items:{uuid.uuid4().hex}",
                title=self.tr("Import JW Library Playlist"),
                initial_stage=self.tr("Opening playlist…"),
                runner=self._jwl_items_import_runner(normalized, section_id),
                completed=commit,
            )
        )

    def _choose_export_path(self, name: str, playlist_format: str) -> str:
        stem = _safe_filename(name)
        if playlist_format == "solin":
            extension = ".solinplaylist"
            title = self.tr("Export Solin Playlist")
            file_filter = "Solin Playlist (*.solinplaylist)"
        else:
            extension = ".jwlplaylist"
            title = self.tr("Export JW Library Playlist")
            file_filter = "JW Library Playlist (*.jwlplaylist)"
        path, _ = QFileDialog.getSaveFileName(
            self._parent_widget,
            title,
            os.path.join(os.path.expanduser("~"), stem + extension),
            file_filter,
        )
        if path and Path(path).suffix.lower() != extension:
            path += extension
        return path

    def _native_export_runner(self, playlist: dict, output_path: str):
        stages = self._native_stage_labels()

        def run(progress, cancellation):
            return export_native_playlist(
                NativePlaylistExportRequest(
                    playlist=playlist,
                    output_path=output_path,
                    media_cache_dir=self._media_cache_manager.media_cache_dir,
                    thumbnail_cache_dir=self._profile_paths.thumb_cache_dir,
                    generator_version=VERSION,
                    progress=PlaylistTransferWorkflow._native_reporter(progress, stages),
                    cancellation=cancellation,
                )
            )

        return run

    def _jwl_export_runner(self, playlist: dict, output_path: str):
        stages = self._jwl_stage_labels()
        fallback_stage = self.tr("Exporting playlist…")
        playlist_name = str(playlist.get("name") or self.tr("Playlist"))
        fallback_language = self._fallback_language_code()

        def run(progress, cancellation):
            def report(phase: str, completed: int, total: int | None) -> None:
                progress(
                    PlaylistTransferProgress(
                        stage=stages.get(phase, fallback_stage),
                        completed=completed,
                        total=total or 0,
                    )
                )

            return export_jwlplaylist_document(
                JwlPlaylistExportRequest(
                    name=playlist_name,
                    items=list(playlist.get("items", [])),
                    output_path=output_path,
                    media_cache_dir=self._media_cache_manager.media_cache_dir,
                    fallback_lang_code=fallback_language,
                    progress_callback=report,
                    should_cancel=cancellation.is_set,
                )
            )

        return run

    def _native_import_job(
        self,
        paths: list[str],
        *,
        open_after: bool,
    ) -> PlaylistTransferJob:
        importing_stage = self.tr("Importing Solin playlist…")
        stages = self._native_stage_labels()
        reserved_names = PlaylistNameRegistry(copy.deepcopy(self._playlists))

        def run(progress, cancellation):
            results: list[NativePlaylistImportResult] = []
            try:
                for index, path in enumerate(paths):
                    progress(
                        PlaylistTransferProgress(
                            stage=importing_stage,
                            detail=Path(path).name,
                            completed=index,
                            total=len(paths),
                        )
                    )
                    results.append(
                        import_native_playlist(
                            NativePlaylistImportRequest(
                                input_path=path,
                                embedded_media_dir=self._profile_paths.embedded_dir,
                                thumbnail_cache_dir=self._profile_paths.thumb_cache_dir,
                                progress=PlaylistTransferWorkflow._native_reporter(
                                    progress, stages
                                ),
                                cancellation=cancellation,
                                validate_playlist_name=reserved_names.reserve,
                            )
                        )
                    )
                return tuple(results)
            except Exception:  # noqa: BLE001 - native batch rollback boundary
                for result in reversed(results):
                    rollback_native_playlist_import(result)
                raise

        def commit(value: object) -> None:
            if not isinstance(value, tuple) or not all(
                isinstance(result, NativePlaylistImportResult) for result in value
            ):
                raise TypeError("Invalid native playlist import result")
            results = value
            imported = [result.playlist for result in results]
            try:
                self._normalize_imported_playlist_names(imported)
            except Exception:  # noqa: BLE001 - native import rollback boundary
                for result in reversed(results):
                    rollback_native_playlist_import(result)
                raise
            previous_count = len(self._playlists)
            self._playlists.extend(imported)
            try:
                self._playlist_repository.save_strict(self._playlists)
            except Exception:  # noqa: BLE001 - persistence rollback boundary
                del self._playlists[previous_count:]
                for result in reversed(results):
                    rollback_native_playlist_import(result)
                raise
            self._refresh_playlists()
            if open_after and imported:
                self._open_playlist(str(imported[-1]["id"]))

        count = len(paths)
        return PlaylistTransferJob(
            key=f"native-import:{uuid.uuid4().hex}",
            title=self.tr("Import Solin Playlist"),
            initial_stage=self.tr("Validating playlist…"),
            runner=run,
            completed=commit,
            succeeded_message=(
                self.tr("1 playlist imported")
                if count == 1
                else self.tr("{count} playlists imported").replace("{count}", str(count))
            ),
        )

    def _jwl_import_job(
        self,
        paths: list[str],
        *,
        open_after: bool,
    ) -> PlaylistTransferJob:
        def commit(value: object) -> None:
            if not isinstance(value, _JwlImportResult):
                raise TypeError("Invalid JWL import result")
            try:
                self._normalize_imported_playlist_names(value.playlists)
            except Exception:  # noqa: BLE001 - imported-media rollback boundary
                self._rollback_profile_files(value.created_files)
                raise
            previous_count = len(self._playlists)
            self._playlists.extend(value.playlists)
            try:
                self._playlist_repository.save_strict(self._playlists)
            except Exception:  # noqa: BLE001 - persistence rollback boundary
                del self._playlists[previous_count:]
                self._rollback_profile_files(value.created_files)
                raise
            self._refresh_playlists()
            if open_after and value.playlists:
                self._open_playlist(str(value.playlists[-1]["id"]))

        count = len(paths)
        return PlaylistTransferJob(
            key=f"jwl-import:{uuid.uuid4().hex}",
            title=self.tr("Import JW Library Playlist"),
            initial_stage=self.tr("Opening playlist…"),
            runner=self._jwl_playlist_import_runner(paths),
            completed=commit,
            succeeded_message=(
                self.tr("1 playlist imported")
                if count == 1
                else self.tr("{count} playlists imported").replace("{count}", str(count))
            ),
        )

    def _jwl_playlist_import_runner(self, paths: list[str]):
        fallback = self._fallback_language_code()
        stages = self._jwl_stage_labels()
        reserved_names = PlaylistNameRegistry(copy.deepcopy(self._playlists))

        def run(progress, cancellation):
            playlists: list[dict] = []
            created: list[Path] = []
            try:
                for path in paths:
                    document = read_jwlplaylist_document(
                        path,
                        fallback_lang_code=fallback,
                        progress_callback=self._jwl_reporter(
                            progress, stages, Path(path).name
                        ),
                        should_cancel=cancellation.is_set,
                    )
                    name = reserved_names.reserve(document.name or Path(path).stem)
                    result = playlist_items_from_jwl_document_items(
                        document.items,
                        source_name=Path(path).name,
                        save_embedded=self._tracked_media_saver(created),
                    )
                    playlists.append(
                        {
                            "id": str(uuid.uuid4()),
                            "name": name,
                            "items": result.items,
                        }
                    )
                return _JwlImportResult(tuple(playlists), tuple(created))
            except Exception:  # noqa: BLE001 - imported-media rollback boundary
                self._rollback_profile_files(created)
                raise

        return run

    def _jwl_items_import_runner(self, paths: list[str], section_id: str):
        fallback = self._fallback_language_code()
        stages = self._jwl_stage_labels()

        def run(progress, cancellation):
            items: list[dict] = []
            created: list[Path] = []
            try:
                for path in paths:
                    document = read_jwlplaylist_document(
                        path,
                        fallback_lang_code=fallback,
                        progress_callback=self._jwl_reporter(
                            progress, stages, Path(path).name
                        ),
                        should_cancel=cancellation.is_set,
                    )
                    result = playlist_items_from_jwl_document_items(
                        document.items,
                        source_name=Path(path).name,
                        section_id=section_id,
                        save_embedded=self._tracked_media_saver(created),
                    )
                    items.extend(result.items)
                return _JwlItemsImportResult(tuple(items), tuple(created))
            except Exception:  # noqa: BLE001 - imported-media rollback boundary
                self._rollback_profile_files(created)
                raise

        return run

    def _tracked_media_saver(self, created: list[Path]):
        def save(
            data: bytes,
            filename: str,
            identifier: str,
            default_suffix: str,
        ) -> str:
            path = self._profile_media_store.save_embedded(
                data,
                filename,
                identifier=identifier,
                default_suffix=default_suffix,
            )
            created.append(Path(path))
            return path

        return save

    @staticmethod
    def _jwl_reporter(progress, stages: dict[str, str], detail: str):
        def report(phase: str, completed: int, total: int | None) -> None:
            progress(
                PlaylistTransferProgress(
                    stage=stages.get(phase, stages["opening"]),
                    detail=detail,
                    completed=completed,
                    total=total or 0,
                )
            )

        return report

    def _jwl_stage_labels(self) -> dict[str, str]:
        return {
            "opening": self.tr("Opening playlist…"),
            "items": self.tr("Processing items…"),
            "database": self.tr("Processing playlist database…"),
            "media_items": self.tr("Reading embedded media…"),
            "media_bytes": self.tr("Reading embedded media…"),
            "archive": self.tr("Writing playlist archive…"),
        }

    def _native_stage_labels(self) -> dict[str, str]:
        return {
            "Preparing playlist": self.tr("Preparing playlist…"),
            "Embedding media": self.tr("Embedding media…"),
            "Finalizing export": self.tr("Finalizing export…"),
            "Validating playlist": self.tr("Validating playlist…"),
            "Resolving JW media": self.tr("Resolving JW media…"),
            "Importing media": self.tr("Importing media…"),
            "Finalizing import": self.tr("Finalizing import…"),
        }

    @staticmethod
    def _native_reporter(progress, stages: dict[str, str]):
        def report(value) -> None:
            progress(
                PlaylistTransferProgress(
                    stage=stages.get(value.stage, value.stage),
                    detail=value.detail,
                    completed=value.completed,
                    total=value.total,
                    can_cancel=value.can_cancel,
                )
            )

        return report

    def _fallback_language_code(self) -> str:
        return jw_media_language_context(self._language_manager).fallback_code

    def _rollback_profile_files(self, paths) -> None:
        for path in reversed(tuple(paths)):
            self._profile_media_store.remove_file(path)

    def _normalize_imported_playlist_names(self, playlists) -> None:
        registry = PlaylistNameRegistry(self._playlists)
        normalized = [registry.reserve(playlist.get("name")) for playlist in playlists]
        for playlist, name in zip(playlists, normalized, strict=True):
            playlist["name"] = name

    def _submit(self, job: PlaylistTransferJob) -> None:
        if not self._coordinator.submit(job):
            self._notifications.warning(
                self.tr("Another playlist transfer is already in progress.")
            )


def _safe_filename(value: str) -> str:
    sanitized = _INVALID_FILENAME_CHARS.sub("_", value).strip(" ._")
    return sanitized[:120] or "playlist"


__all__ = ["PlaylistTransferWorkflow"]
