"""Composition boundary for meeting-tree controllers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject

from solin.core.jw.language_context import JWMediaLanguageContext
from solin.widgets.meetings.tree_controller import MeetingTreeController


@dataclass(frozen=True, slots=True)
class MeetingTreeControllerDependencies:
    service: Any
    store: Any
    profile_media_store: Any
    meeting_thumbnail_store: Any
    watched_folder_file_store: Any
    jwpub_import_thread_factory: Any
    document_conversion_service: Any
    profile_paths: Any
    runtime_paths: Any
    cache_manager: Any
    media_tree_runtime: Any
    linked_folder_sync: Any
    media_info_queue_factory: Callable[[QObject], Any]
    projection_aspect_ratio_provider: Callable[[], object] | None = None


class MeetingTreeControllerFactory:
    """Build identical live and headless meeting-tree controllers."""

    def __init__(self, dependencies: MeetingTreeControllerDependencies) -> None:
        self._dependencies = dependencies

    def create(
        self,
        meeting_type: str,
        language_context: JWMediaLanguageContext,
        *,
        parent: QObject,
    ) -> MeetingTreeController:
        dependencies = self._dependencies
        return MeetingTreeController(
            dependencies.service,
            meeting_type=meeting_type,
            language_code=language_context.api_code,
            store=dependencies.store,
            profile_media_store=dependencies.profile_media_store,
            meeting_thumbnail_store=dependencies.meeting_thumbnail_store,
            watched_folder_file_store=dependencies.watched_folder_file_store,
            jwpub_import_thread_factory=dependencies.jwpub_import_thread_factory,
            document_conversion_service=dependencies.document_conversion_service,
            profile_paths=dependencies.profile_paths,
            runtime_paths=dependencies.runtime_paths,
            cache_manager=dependencies.cache_manager,
            media_tree_runtime=dependencies.media_tree_runtime,
            linked_folder_sync=dependencies.linked_folder_sync,
            media_info_queue_factory=dependencies.media_info_queue_factory,
            projection_aspect_ratio_provider=(dependencies.projection_aspect_ratio_provider),
            fallback_language_code=language_context.fallback_code,
            parent=parent,
        )


__all__ = ["MeetingTreeControllerDependencies", "MeetingTreeControllerFactory"]
