"""Application coordinator for the unified media destination workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import uuid4

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QTimer,
    QT_TRANSLATE_NOOP,
    Signal,
    Slot,
)
from PySide6.QtWidgets import QDialog

from solin.core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationKind,
    MediaDestinationOutcome,
    MediaDestinationRequest,
    MediaRouteAction,
    MeetingDestinationTarget,
    PlaylistDestinationTarget,
    PreparedMediaBatch,
)
from solin.core.media.formats import media_type_from_path
from solin.core.media.identity import media_identity
from solin.core.media.insertion import MediaInsertResult
from solin.core.media.placement import build_media_placement_options
from solin.core.i18n.media_placement import translate_media_placement
from solin.core.playlists.items import create_playlist_item
from solin.ui.media_insertion_feedback import (
    notify_media_duplicate,
    tr_media_already_added_count,
)
from solin.ui.qml.media_destination import (
    MediaDestinationBridge,
    MediaDestinationDialog,
)
from solin.widgets.meetings.destinations import MeetingDestinationSession

PreparedCallback = Callable[[MediaDestinationRequest], None]
FailedCallback = Callable[[str], None]
PrepareMedia = Callable[[PreparedCallback, FailedCallback], None]
CompletionCallback = Callable[[MediaDestinationOutcome], None]
DialogFactory = Callable[[MediaDestinationBridge, Any], Any]


class MediaDestinationNotifications(QObject):
    """Qt translation context for destination notifications."""

    @staticmethod
    def items_added_to_meeting(count: int) -> str:
        return MediaDestinationNotifications.tr(
            "%n item(s) added to the meeting",
            "",
            max(0, int(count)),
        )


_TR_CONTEXT = MediaDestinationNotifications.__name__
_PREPARATION_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "Could not prepare this media.",
)
_PLAYLIST_UNAVAILABLE_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "This playlist is no longer available. Choose another one.",
)
_INVALID_WEEK_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "Invalid meeting week.",
)
_MEETING_NOT_READY_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "This meeting is not available yet. Try loading the week again.",
)
_MEETING_UNAVAILABLE_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "The selected meeting is no longer available.",
)
_NO_MEDIA_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDestinationNotifications",
    "No media was available to add.",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


@dataclass(frozen=True, slots=True)
class MediaDestinationContext:
    dialog_parent: Any
    playlist_widget: Any
    meetings_widget: Any
    playlist_imports: Any
    notifications: Any
    dialog_factory: DialogFactory = MediaDestinationDialog


class MediaDestinationController(QObject):
    """Coordinate preparation, target selection, insertion, and source cleanup."""

    _prepared = Signal(str, object)
    _preparation_failed = Signal(str, str)

    def __init__(self, context: MediaDestinationContext, parent=None) -> None:
        super().__init__(parent)
        self._context = context
        self._active_token = ""
        self._active_bridge: MediaDestinationBridge | None = None
        self._active_request: MediaDestinationRequest | None = None
        self._active_prepare: PrepareMedia | None = None
        self._pending_meeting_session: MeetingDestinationSession | None = None
        self._pending_meeting_key: tuple[str, str] | None = None
        self._prepared.connect(self._on_prepared)
        self._preparation_failed.connect(self._on_preparation_failed)
        context.meetings_widget.destinationTargetsChanged.connect(
            self._on_destination_targets_changed
        )

    def route(
        self,
        request: MediaDestinationRequest,
        *,
        play: Callable[[], None] | None = None,
        prepare: PrepareMedia | None = None,
        completed: CompletionCallback | None = None,
    ) -> None:
        """Run one modal route from source action through durable insertion."""

        if self._active_bridge is not None:
            return

        token = uuid4().hex
        bridge = MediaDestinationBridge(
            media_title=request.title,
            item_count=request.item_count,
            can_play=request.can_play,
            playlists=self._context.playlist_widget.get_playlist_names(),
            parent=self,
        )
        self._active_token = token
        self._active_bridge = bridge
        self._active_request = request
        self._active_prepare = prepare

        bridge.addRequested.connect(lambda: self._start_preparation(token))
        bridge.retryRequested.connect(lambda: self._start_preparation(token))
        bridge.playlistTargetRequested.connect(self._request_playlist_target)
        bridge.weekRequested.connect(self._request_week)
        bridge.meetingTargetRequested.connect(self._request_meeting_target)
        bridge.meetingDownloadRequested.connect(self._download_meeting)

        dialog = self._context.dialog_factory(bridge, self._context.dialog_parent)
        if not request.can_play:
            if prepare is None:
                bridge.showDestinations()
            else:
                bridge.showPreparing()
                QTimer.singleShot(0, lambda: self._start_preparation(token))

        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selection = dialog.selection if accepted else None
        prepared_request = self._active_request
        self._clear_active_dialog()

        if selection is None or prepared_request is None:
            self._close_pending_meeting_session()
            return
        if selection.action == MediaRouteAction.PLAY:
            self._close_pending_meeting_session()
            if play is not None:
                play()
            return
        if prepared_request.select_import_contents:
            self._select_import_contents(prepared_request, selection.target, completed)
            return
        if selection.destination == MediaDestinationKind.PLAYLIST:
            self._close_pending_meeting_session()
            self._add_to_playlist(prepared_request, selection.target, completed)
            return
        if selection.destination == MediaDestinationKind.MEETING:
            self._add_to_meeting(prepared_request, selection.target, completed)
            return
        self._close_pending_meeting_session()

    def route_projected_media(self, url: str, title: str, meta: object) -> None:
        if not url:
            return
        metadata = dict(meta) if isinstance(meta, dict) else {}
        media_type = str(metadata.pop("type", "")) or media_type_from_path(
            url,
            default="video",
        )
        item = create_playlist_item(
            title=title,
            url=url,
            type=media_type,
            **metadata,
        )
        self.route(
            MediaDestinationRequest(
                title=title,
                assets=(
                    MediaDestinationAsset(
                        title=title,
                        source_id=url,
                        item=item,
                    ),
                ),
            )
        )

    def _start_preparation(self, token: str) -> None:
        if token != self._active_token or self._active_bridge is None:
            return
        if self._active_prepare is None:
            self._active_bridge.showDestinations()
            return
        self._active_bridge.showPreparing()
        try:
            self._active_prepare(
                lambda request: self._prepared.emit(token, request),
                lambda error: self._preparation_failed.emit(token, str(error)),
            )
        except (OSError, ValueError, RuntimeError) as error:
            self._preparation_failed.emit(token, str(error))

    @Slot(str, object)
    def _on_prepared(self, token: str, request: object) -> None:
        if (
            token != self._active_token
            or self._active_bridge is None
            or not isinstance(request, MediaDestinationRequest)
        ):
            return
        self._active_request = request
        self._active_prepare = None
        self._active_bridge.showDestinations()

    @Slot(str, str)
    def _on_preparation_failed(self, token: str, error: str) -> None:
        if token != self._active_token or self._active_bridge is None:
            return
        message = error.strip() or _tr(_PREPARATION_ERROR_SOURCE)
        self._active_bridge.showError(message)

    @Slot(str, bool)
    def _request_week(self, monday_text: str, force: bool) -> None:
        bridge = self._active_bridge
        if bridge is None:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            bridge.update_meeting_targets(monday_text, [])
            return
        meetings = self._context.meetings_widget
        bridge.update_meeting_targets(
            monday_text,
            meetings.destination_targets(monday),
        )
        meetings.request_destination_week(monday, force=force)

    @Slot(str, str, bool)
    def _download_meeting(self, pub_type: str, monday_text: str, force: bool) -> None:
        if self._active_bridge is None or pub_type not in {"mwb", "wt"}:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            return
        self._context.meetings_widget.prepare_destination_meeting(monday, pub_type, force=force)

    @Slot(str)
    def _on_destination_targets_changed(self, monday_text: str) -> None:
        bridge = self._active_bridge
        if bridge is None or bridge.weekMonday != monday_text:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            return
        bridge.update_meeting_targets(
            monday_text,
            self._context.meetings_widget.destination_targets(monday),
        )

    @Slot(str, str)
    def _request_playlist_target(
        self,
        playlist_id: str,
        playlist_name: str,
    ) -> None:
        bridge = self._active_bridge
        if bridge is None:
            return
        playlist_ref = self._context.playlist_widget.playlist_placement_ref(
            playlist_id
        )
        if playlist_ref is None:
            bridge.showError(
                _tr(_PLAYLIST_UNAVAILABLE_SOURCE)
            )
            return
        options = [] if self._selects_import_contents() else build_media_placement_options(
            playlist_ref,
            translate=translate_media_placement,
        )
        bridge.prepare_playlist_selection(
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            placement_options=options,
        )

    @Slot(str, str)
    def _request_meeting_target(self, pub_type: str, monday_text: str) -> None:
        bridge = self._active_bridge
        if bridge is None:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            bridge.show_meeting_error(_tr(_INVALID_WEEK_SOURCE))
            return

        self._close_pending_meeting_session()
        session = self._context.meetings_widget.open_destination_session(
            monday,
            pub_type,
        )
        if session is None:
            bridge.show_meeting_error(
                _tr(_MEETING_NOT_READY_SOURCE)
            )
            return
        self._pending_meeting_session = session
        self._pending_meeting_key = (monday_text, pub_type)
        session.closed.connect(self._on_meeting_session_closed)
        options = [] if self._selects_import_contents() else build_media_placement_options(
            session.placement_ref(),
            translate=translate_media_placement,
        )
        bridge.prepare_meeting_selection(
            monday=monday_text,
            pub_type=pub_type,
            placement_options=options,
        )

    def _on_meeting_session_closed(self, session: MeetingDestinationSession) -> None:
        if self._pending_meeting_session is not session:
            return
        self._pending_meeting_session = None
        self._pending_meeting_key = None
        if self._active_bridge is not None:
            self._active_bridge.show_meeting_error(_tr(_MEETING_UNAVAILABLE_SOURCE))

    def _selects_import_contents(self) -> bool:
        return bool(self._active_request and self._active_request.select_import_contents)

    def _select_import_contents(
        self,
        request: MediaDestinationRequest,
        target: object,
        completed: CompletionCallback | None,
    ) -> None:
        paths = [asset.import_path for asset in request.assets if asset.import_path]
        if isinstance(target, PlaylistDestinationTarget):
            self._close_pending_meeting_session()
            outcome = self._context.playlist_widget.import_publications_to_playlist_target(
                paths, target,
            )
        elif isinstance(target, MeetingDestinationTarget):
            session = self._take_meeting_session(target)
            if session is None:
                return
            try:
                outcome = session.import_publications(paths)
            finally:
                session.close()
        else:
            self._close_pending_meeting_session()
            return
        if outcome is not None and completed is not None:
            completed(MediaDestinationOutcome(
                outcome.added_count,
                outcome.referenced_urls,
                tuple(asset.source_id for asset in request.assets)
                if outcome.destination_accepted else (),
                outcome.destination_accepted,
            ))

    def _take_meeting_session(
        self, target: MeetingDestinationTarget,
    ) -> MeetingDestinationSession | None:
        session = self._pending_meeting_session
        if self._pending_meeting_key != (target.monday, target.pub_type):
            self._close_pending_meeting_session()
            try:
                monday = date.fromisoformat(target.monday)
            except ValueError:
                return None
            session = self._context.meetings_widget.open_destination_session(monday, target.pub_type)
        self._pending_meeting_session = None
        self._pending_meeting_key = None
        if session is None:
            self._context.notifications.error(_tr(_MEETING_UNAVAILABLE_SOURCE))
        return session

    def _add_to_playlist(
        self,
        request: MediaDestinationRequest,
        raw_target: object,
        completed: CompletionCallback | None,
    ) -> None:
        if not isinstance(raw_target, PlaylistDestinationTarget):
            return

        def add_prepared(batch: PreparedMediaBatch) -> None:
            if batch.items:
                outcome = self._context.playlist_imports.add_items_to_playlist_target(
                    raw_target,
                    [dict(item) for item in batch.items],
                    request.title,
                )
                outcome = MediaDestinationOutcome(
                    outcome.added_count,
                    outcome.referenced_urls,
                    (
                        batch.handled_sources
                        if outcome.destination_accepted
                        else ()
                    ),
                    outcome.destination_accepted,
                )
            else:
                outcome = MediaDestinationOutcome(
                    0,
                    handled_sources=batch.handled_sources,
                )
            if completed is not None:
                completed(outcome)

        self._prepare_assets(request, add_prepared)

    def _add_to_meeting(
        self,
        request: MediaDestinationRequest,
        raw_target: object,
        completed: CompletionCallback | None,
    ) -> None:
        if not isinstance(raw_target, MeetingDestinationTarget):
            self._close_pending_meeting_session()
            return
        session = self._take_meeting_session(raw_target)
        if session is None:
            return

        def add_prepared(batch: PreparedMediaBatch) -> None:
            if session.is_closed:
                self._context.notifications.error(_tr(_MEETING_UNAVAILABLE_SOURCE))
                if completed is not None:
                    completed(MediaDestinationOutcome(0, destination_accepted=False))
                return
            if not batch.items:
                session.close()
                if completed is not None:
                    completed(
                        MediaDestinationOutcome(
                            0,
                            handled_sources=batch.handled_sources,
                        )
                    )
                return
            session.completed.connect(lambda result: finish(result, batch))
            session.failed.connect(fail)
            session.add_items(
                [dict(item) for item in batch.items],
                list_id=raw_target.list_id,
                insert_index=raw_target.insert_index,
            )

        def finish(result: MediaInsertResult, batch: PreparedMediaBatch) -> None:
            count = result.added_count
            urls = tuple(
                str(item.get("url") or "")
                for item in result.added_items
                if item.get("url")
            )
            if count:
                self._context.notifications.success(
                    MediaDestinationNotifications.items_added_to_meeting(count)
                )
            if result.duplicate_count == 1:
                duplicate = result.duplicate_items[0]
                identity = media_identity(duplicate)
                notify_media_duplicate(
                    self._context.notifications,
                    str(duplicate.get("title") or ""),
                    identity.dedupe_token if identity is not None else "unknown",
                )
            elif result.duplicate_count > 1:
                self._context.notifications.warning(
                    tr_media_already_added_count(result.duplicate_count)
                )
            if completed is not None:
                completed(
                    MediaDestinationOutcome(
                        count,
                        urls,
                        batch.handled_sources,
                    )
                )
            session.close()

        def fail(message: str) -> None:
            self._context.notifications.error(message)
            session.close()

        self._prepare_assets(request, add_prepared)

    def _prepare_assets(
        self,
        request: MediaDestinationRequest,
        completed: Callable[[PreparedMediaBatch], None],
    ) -> None:
        if not request.assets:
            self._context.notifications.error(
                _tr(_NO_MEDIA_SOURCE)
            )
            completed(PreparedMediaBatch())
            return
        self._context.playlist_imports.prepare_destination_assets(
            list(request.assets),
            completed,
        )

    def _close_pending_meeting_session(self) -> None:
        session = self._pending_meeting_session
        self._pending_meeting_session = None
        self._pending_meeting_key = None
        if session is not None:
            session.close()

    def _clear_active_dialog(self) -> None:
        self._active_token = ""
        self._active_bridge = None
        self._active_request = None
        self._active_prepare = None


__all__ = ["MediaDestinationContext", "MediaDestinationController"]
