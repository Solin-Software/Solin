"""QML bridge and host for the unified media destination wizard."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from PySide6.QtCore import QPoint, Property, QObject, QRect, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QDialog, QVBoxLayout

from solin.core.i18n.date import week_label
from solin.core.media.destinations import (
    MediaDestinationKind,
    MediaDestinationSelection,
    MediaRouteAction,
    MeetingDestinationTarget,
    PlaylistDestinationTarget,
)
from solin.core.media.placement import resolve_media_placement
from solin.core.meetings.meeting_weeks import (
    current_monday,
    selectable_meeting_weeks,
)
from solin.styles.theme import PALETTE
from solin.styles.icons import (
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_CLOSE,
    ICON_NAV_MEETINGS,
    ICON_NAV_PLAYLIST,
    ICON_PLAY,
    ICON_PLUS,
)
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.svg_icons import SvgIconProvider

DESTINATION_ICON_SVGS = {
    "chevron_left": ICON_CHEVRON_LEFT,
    "chevron_right": ICON_CHEVRON_RIGHT,
    "close": ICON_CLOSE,
    "meeting": ICON_NAV_MEETINGS,
    "play": ICON_PLAY,
    "playlist": ICON_NAV_PLAYLIST,
    "plus": ICON_PLUS,
}


class MediaDestinationBridge(QObject):
    """Own wizard navigation while delegating all mutations to its coordinator."""

    changed = Signal()
    finished = Signal()
    addRequested = Signal()
    retryRequested = Signal()
    weekRequested = Signal(str, bool)
    meetingTargetRequested = Signal(str, str)

    def __init__(
        self,
        *,
        media_title: str,
        item_count: int,
        can_play: bool,
        playlists: Iterable[tuple[str, str]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._media_title = media_title
        self._item_count = max(1, item_count)
        self._can_play = can_play
        self._step = "action" if can_play else "destination"
        self._history: list[str] = []
        self._busy = False
        self._error_text = ""
        self._error_retry = "preparation"
        self._playlists = [
            {"id": playlist_id, "name": playlist_name} for playlist_id, playlist_name in playlists
        ]
        self._weeks = selectable_meeting_weeks()
        self._week_index = self._weeks.index(current_monday())
        self._meeting_targets: list[dict[str, Any]] = []
        self._placement_options: list[dict[str, Any]] = []
        self._pending_meeting_type = ""
        self._pending_meeting_monday = ""
        self._result: MediaDestinationSelection | None = None
        self._completed = False

    @Property(str, notify=changed)
    def step(self) -> str:
        return self._step

    @Property(str, notify=changed)
    def mediaTitle(self) -> str:  # noqa: N802 - QML API
        return self._media_title

    @Property(str, notify=changed)
    def itemSummary(self) -> str:  # noqa: N802 - QML API
        if self._item_count == 1:
            return self.tr("1 item")
        return self.tr("%n items", "", self._item_count)

    @Property(bool, notify=changed)
    def canPlay(self) -> bool:  # noqa: N802 - QML API
        return self._can_play

    @Property(bool, notify=changed)
    def canGoBack(self) -> bool:  # noqa: N802 - QML API
        return bool(self._history) and not self._busy

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=changed)
    def errorText(self) -> str:  # noqa: N802 - QML API
        return self._error_text

    @Property(list, notify=changed)
    def playlists(self) -> list[dict[str, str]]:
        return list(self._playlists)

    @Property(list, notify=changed)
    def meetingTargets(self) -> list[dict[str, Any]]:  # noqa: N802 - QML API
        return list(self._meeting_targets)

    @Property(list, notify=changed)
    def placementOptions(self) -> list[dict[str, Any]]:  # noqa: N802 - QML API
        return list(self._placement_options)

    @Property(bool, notify=changed)
    def showPlacement(self) -> bool:  # noqa: N802 - shared placement API
        return self._step == "placement"

    @Property(str, notify=changed)
    def pendingItemTitle(self) -> str:  # noqa: N802 - shared placement API
        return self._media_title

    @Property(str, notify=changed)
    def pendingItemThumb(self) -> str:  # noqa: N802 - shared placement API
        return ""

    @Property(str, notify=changed)
    def weekMonday(self) -> str:  # noqa: N802 - QML API
        return self._selected_week().isoformat()

    @Property(str, notify=changed)
    def weekLabel(self) -> str:  # noqa: N802 - QML API
        return week_label(self._selected_week())

    @Property(bool, notify=changed)
    def isCurrentWeek(self) -> bool:  # noqa: N802 - QML API
        return self._selected_week() == current_monday()

    @Property(bool, notify=changed)
    def canPreviousWeek(self) -> bool:  # noqa: N802 - QML API
        return self._week_index > 0 and not self._busy

    @Property(bool, notify=changed)
    def canNextWeek(self) -> bool:  # noqa: N802 - QML API
        return self._week_index < len(self._weeks) - 1 and not self._busy

    @property
    def result(self) -> MediaDestinationSelection | None:
        return self._result

    @property
    def current_step(self) -> str:
        return self._step

    @Slot()
    def choosePlay(self) -> None:  # noqa: N802 - QML API
        if not self._can_play or self._busy:
            return
        self._finish(MediaDestinationSelection(action=MediaRouteAction.PLAY))

    @Slot()
    def requestAdd(self) -> None:  # noqa: N802 - QML API
        if self._busy:
            return
        self.addRequested.emit()

    @Slot()
    def showDestinations(self) -> None:  # noqa: N802 - QML API
        if self._step == "preparing":
            self._step = "destination"
        else:
            self._push_step("destination")
            return
        self._busy = False
        self._error_text = ""
        self.changed.emit()

    @Slot(str)
    def showPreparing(self, message: str = "") -> None:  # noqa: N802 - QML API
        del message
        is_initial_add_preparation = (
            self._step == "destination" and not self._can_play and not self._history
        )
        if self._step not in {"preparing", "error"} and not is_initial_add_preparation:
            self._history.append(self._step)
        self._step = "preparing"
        self._busy = True
        self._error_text = ""
        self.changed.emit()

    @Slot(str)
    def showError(self, message: str) -> None:  # noqa: N802 - QML API
        self._show_error(message, retry="preparation")

    def show_meeting_error(self, message: str) -> None:
        self._show_error(message, retry="meeting")

    def _show_error(self, message: str, *, retry: str) -> None:
        self._step = "error"
        self._busy = False
        self._error_text = message
        self._error_retry = retry
        self.changed.emit()

    @Slot()
    def retry(self) -> None:
        if self._busy:
            return
        if self._error_retry == "meeting":
            self._step = "meeting"
            self._error_text = ""
            self.changed.emit()
            self.request_current_week(force=True)
            return
        self.retryRequested.emit()

    @Slot()
    def showPlaylists(self) -> None:  # noqa: N802 - QML API
        self._push_step("playlist")

    @Slot()
    def showMeetings(self) -> None:  # noqa: N802 - QML API
        self._push_step("meeting")
        self.request_current_week()

    @Slot(str, str)
    def choosePlaylist(self, playlist_id: str, playlist_name: str) -> None:  # noqa: N802
        if self._busy or not playlist_id:
            return
        self._finish(
            MediaDestinationSelection(
                action=MediaRouteAction.ADD,
                destination=MediaDestinationKind.PLAYLIST,
                target=PlaylistDestinationTarget(playlist_id, playlist_name),
            )
        )

    @Slot(str)
    def createPlaylist(self, playlist_name: str) -> None:  # noqa: N802 - QML API
        name = playlist_name.strip()
        if self._busy or not name:
            return
        self._finish(
            MediaDestinationSelection(
                action=MediaRouteAction.ADD,
                destination=MediaDestinationKind.PLAYLIST,
                target=PlaylistDestinationTarget("", name, create_new=True),
            )
        )

    @Slot()
    def previousWeek(self) -> None:  # noqa: N802 - QML API
        self._set_week_index(self._week_index - 1)

    @Slot()
    def nextWeek(self) -> None:  # noqa: N802 - QML API
        self._set_week_index(self._week_index + 1)

    @Slot()
    def currentWeek(self) -> None:  # noqa: N802 - QML API
        self._set_week_index(self._weeks.index(current_monday()))

    @Slot()
    def retryWeek(self) -> None:  # noqa: N802 - QML API
        self.request_current_week(force=True)

    @Slot(str)
    def chooseMeeting(self, pub_type: str) -> None:  # noqa: N802 - QML API
        if self._busy or pub_type not in {"mwb", "wt"}:
            return
        target = next(
            (target for target in self._meeting_targets if target.get("pub_type") == pub_type),
            None,
        )
        if not target or not target.get("available"):
            return
        self._busy = True
        self.changed.emit()
        self.meetingTargetRequested.emit(pub_type, self.weekMonday)

    def update_meeting_targets(
        self,
        monday: str,
        targets: Iterable[dict[str, Any]],
    ) -> None:
        if monday != self.weekMonday:
            return
        self._meeting_targets = [dict(target) for target in targets]
        self._busy = False
        self.changed.emit()

    def prepare_meeting_selection(
        self,
        *,
        monday: str,
        pub_type: str,
        placement_options: Iterable[Mapping[str, Any]],
    ) -> None:
        self._busy = False
        self._pending_meeting_monday = monday
        self._pending_meeting_type = pub_type
        self._placement_options = [dict(option) for option in placement_options]
        if not self._placement_options:
            self._finish_meeting("bottom")
            return
        self._push_step("placement")

    @Slot(str)
    def confirmPlacement(self, placement_id: str) -> None:  # noqa: N802 - QML API
        if self._busy or not self._pending_meeting_type:
            return
        valid_ids = {str(option.get("id") or "") for option in self._placement_options}
        if placement_id not in valid_ids:
            return
        self._finish_meeting(placement_id)

    @Slot()
    def cancelSelection(self) -> None:  # noqa: N802 - shared placement API
        self.back()

    @Slot()
    def back(self) -> None:
        if not self._history or self._busy:
            return
        self._step = self._history.pop()
        self._error_text = ""
        self.changed.emit()
        if self._step == "meeting":
            self.request_current_week()

    @Slot()
    def cancel(self) -> None:
        if self._completed:
            return
        self._completed = True
        self._result = None
        self.finished.emit()

    def request_current_week(self, *, force: bool = False) -> None:
        self._meeting_targets = []
        self.changed.emit()
        self.weekRequested.emit(self.weekMonday, force)

    def _selected_week(self) -> date:
        return self._weeks[self._week_index]

    def _set_week_index(self, index: int) -> None:
        if self._busy or index < 0 or index >= len(self._weeks):
            return
        if index == self._week_index:
            self.request_current_week()
            return
        self._week_index = index
        self._meeting_targets = []
        self.changed.emit()
        self.request_current_week()

    def _push_step(self, step: str) -> None:
        if self._step == step:
            return
        self._history.append(self._step)
        self._step = step
        self._error_text = ""
        self.changed.emit()

    def _finish_meeting(self, placement_id: str) -> None:
        list_id, insert_index = resolve_media_placement(placement_id)
        self._finish(
            MediaDestinationSelection(
                action=MediaRouteAction.ADD,
                destination=MediaDestinationKind.MEETING,
                target=MeetingDestinationTarget(
                    monday=self._pending_meeting_monday,
                    pub_type=self._pending_meeting_type,
                    list_id=list_id,
                    insert_index=insert_index,
                ),
            )
        )

    def _finish(self, result: MediaDestinationSelection) -> None:
        if self._completed:
            return
        self._completed = True
        self._result = result
        self.finished.emit()


class MediaDestinationDialog(QDialog):
    """Native modal shell hosting the reusable destination wizard QML."""

    _DIALOG_WIDTH = 460
    _STEP_HEIGHTS = {
        "action": 390,
        "preparing": 350,
        "destination": 390,
        "playlist": 500,
        "meeting": 440,
        "placement": 440,
        "error": 350,
    }

    def __init__(self, bridge: MediaDestinationBridge, parent=None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._initial_position_applied = False
        self._sized_step = ""
        self.setWindowTitle(self.tr("Media destination"))
        self.setModal(True)
        self.setMinimumSize(410, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.qml_widget = QQuickWidget(self)
        configure_qml_host(
            self.qml_widget,
            type_name="MediaDestinationDialog",
            clear_color=PALETTE.bg0,
            context_properties={"destinationBridge": bridge},
            image_providers={
                "destinationicons": SvgIconProvider(
                    DESTINATION_ICON_SVGS,
                    default_icon="plus",
                )
            },
            mouse_tracking=True,
        )
        layout.addWidget(self.qml_widget)
        bridge.finished.connect(self._complete)
        bridge.changed.connect(self._sync_size_for_step)
        self._sync_size_for_step()

    @property
    def selection(self) -> MediaDestinationSelection | None:
        return self._bridge.result

    def reject(self) -> None:
        if self._bridge.result is None:
            self._bridge.cancel()
        else:
            super().reject()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if self._initial_position_applied:
            return
        self._initial_position_applied = True
        QTimer.singleShot(0, self._center_on_parent)

    def _center_on_parent(self) -> None:
        parent = self.parentWidget()
        parent_window = parent.window() if parent is not None else None
        if parent_window is not None and parent_window.isVisible():
            anchor = parent_window.frameGeometry()
            screen = parent_window.screen()
        else:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            anchor = screen.availableGeometry()

        available = screen.availableGeometry() if screen is not None else anchor
        frame_size = self.frameGeometry().size()
        if frame_size.isEmpty():
            frame_size = self.size()
        self.move(centered_dialog_position(anchor, frame_size, available))

    def _sync_size_for_step(self) -> None:
        step = self._bridge.current_step
        if step == self._sized_step:
            return
        self._sized_step = step
        self.resize(
            self._DIALOG_WIDTH,
            self._STEP_HEIGHTS.get(step, self._STEP_HEIGHTS["destination"]),
        )
        if self.isVisible():
            QTimer.singleShot(0, self._center_on_parent)

    @Slot()
    def _complete(self) -> None:
        if self._bridge.result is None:
            super().reject()
        else:
            self.accept()


def centered_dialog_position(
    anchor: QRect,
    dialog_size: QSize,
    available: QRect,
) -> QPoint:
    """Center a dialog on its owner while keeping it inside the owner screen."""

    x = anchor.center().x() - dialog_size.width() // 2
    y = anchor.center().y() - dialog_size.height() // 2
    max_x = available.right() - dialog_size.width() + 1
    max_y = available.bottom() - dialog_size.height() + 1
    return QPoint(
        max(available.left(), min(x, max_x)),
        max(available.top(), min(y, max_y)),
    )


__all__ = [
    "MediaDestinationBridge",
    "MediaDestinationDialog",
    "centered_dialog_position",
]
