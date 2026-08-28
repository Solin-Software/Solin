"""QML bridge facade for PlaylistEditView.

The bridge exposes user intents and lightweight chrome state. Tree data lives
exclusively in ``PlaylistTreeSession.model``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

from solin.core.i18n.strings import tr_item_count
from solin.core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)

if TYPE_CHECKING:
    from solin.ui.qml.media_tree.playlist_session import PlaylistTreeSession


# ── Bridge ─────────────────────────────────────────────────────────────────────

class PlaylistEditBridge(QObject):
    """QML ↔ Python bridge for the playlist edit view.

    Exposes playlist state as Qt properties and provides slots that QML
    delegates call for user interactions (back, add, play, project, etc.).
    Actual business logic remains in ``_PlaylistEditView``.
    """

    # Signals for Python consumers (connected in _PlaylistEditView)
    backRequested          = Signal()
    addRequested           = Signal()
    exportRequested        = Signal(str)
    saveTempRequested      = Signal()
    playAllRequested       = Signal()
    shuffleRequested       = Signal()
    newSectionRequested    = Signal()
    newSubsectionRequested = Signal(str)      # parent_section_id
    newMarkerRequested     = Signal(str)      # subsection_id

    projectItemSignal    = Signal(str)          # item_id
    removeItemSignal     = Signal(str)          # item_id
    renameItemSignal     = Signal(str)          # item_id
    downloadItemSignal   = Signal(str)          # item_id
    imageFramingSetRequested = Signal(
        str, float, float, float, float, float, bool
    )
    imageFramingResetRequested = Signal(str)
    mediaTrimSetRequested = Signal(str, float, float, float)
    operationCancelRequested = Signal(str)
    operationRetryRequested = Signal(str)
    renameMarkerSignal   = Signal(str, str)     # marker_id, text
    deleteMarkerSignal   = Signal(str)          # marker_id

    renameSectionSignal  = Signal(str)          # section_id
    deleteSectionSignal  = Signal(str)          # section_id
    recolorSectionSignal = Signal(str)          # section_id

    collapseSectionSignal = Signal(str)         # section_id

    dragFinished = Signal()                     # trigger save after drag
    stateChanged = Signal()
    chromeChanged = Signal()
    markerEditRequested = Signal(str)           # marker_id
    pointerEntered = Signal()
    pointerCursorEntered = Signal(str, int)
    pointerCursorChanged = Signal(str, int)
    pointerCursorExited = Signal(str)
    pointerExited = Signal()

    def __init__(
        self,
        projection_aspect_ratio_provider: Callable[[], Any] | None = None,
        image_source_aspect_ratio_provider: Callable[[str], float] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._projection_aspect_ratio_provider = (
            projection_aspect_ratio_provider
            or (lambda: DEFAULT_PROJECTION_ASPECT_RATIO)
        )
        self._image_source_aspect_ratio_provider = (
            image_source_aspect_ratio_provider or (lambda _item_id: 0.0)
        )
        self._playlist_name = ""
        self._is_temp = False
        self._is_watched = False
        self._is_loading = False
        self._has_items = False
        self._item_count_text = "0 items"
        self._tree_session: PlaylistTreeSession | None = None

    def attach_session(self, session: PlaylistTreeSession) -> None:
        self._tree_session = session
        self.stateChanged.emit()

    # ── Properties ─────────────────────────────────────────────────────────

    @Property(str, notify=stateChanged)
    def playlistName(self):
        return self._playlist_name

    @Property(bool, notify=chromeChanged)
    def isTemp(self):
        return self._is_temp

    @Property(bool, notify=chromeChanged)
    def isWatched(self):
        return self._is_watched

    @Property(bool, notify=chromeChanged)
    def isLoading(self):
        return self._is_loading

    @Property(bool, notify=chromeChanged)
    def hasItems(self):
        return self._has_items

    @Property(str, notify=chromeChanged)
    def itemCountText(self):
        return self._item_count_text

    # ── State setters (called from Python) ─────────────────────────────────

    def set_state(self, *, name: str = "", is_temp: bool = False,
                  is_watched: bool = False, is_loading: bool = False,
                  item_count: int = 0,
                  has_entries: bool | None = None,
                  emit_data_changed: bool = True) -> None:
        self._playlist_name = name
        self._is_temp = is_temp
        self._is_watched = is_watched
        self._is_loading = is_loading
        self._has_items = (item_count > 0 if has_entries is None else has_entries)
        self._item_count_text = tr_item_count(item_count)
        self.chromeChanged.emit()
        if emit_data_changed:
            self.stateChanged.emit()

    # ── Slots (called from QML) ────────────────────────────────────────────

    @Slot()
    def backClicked(self):
        self.backRequested.emit()

    @Slot()
    def addClicked(self):
        self.addRequested.emit()

    @Slot(str)
    def exportClicked(self, playlist_format: str):
        self.exportRequested.emit(playlist_format)

    @Slot()
    def saveTempClicked(self):
        self.saveTempRequested.emit()

    @Slot()
    def playAllClicked(self):
        self.playAllRequested.emit()

    @Slot()
    def shuffleClicked(self):
        self.shuffleRequested.emit()

    @Slot()
    def newSectionClicked(self):
        self.newSectionRequested.emit()

    @Slot(str)
    def newSubsectionClicked(self, parent_section_id: str):
        self.newSubsectionRequested.emit(parent_section_id)

    @Slot(str)
    def newMarkerClicked(self, subsection_id: str):
        self.newMarkerRequested.emit(subsection_id)

    @Slot(str)
    def projectItem(self, item_id: str):
        self.projectItemSignal.emit(item_id)

    @Slot(str)
    def removeItem(self, item_id: str):
        self.removeItemSignal.emit(item_id)

    @Slot(str)
    def renameItem(self, item_id: str):
        self.renameItemSignal.emit(item_id)

    @Slot(str)
    def downloadItem(self, item_id: str):
        self.downloadItemSignal.emit(item_id)

    @Slot(str)
    def cancelOperation(self, operation_id: str) -> None:  # noqa: N802 - QML API
        self.operationCancelRequested.emit(operation_id)

    @Slot(str)
    def retryOperation(self, operation_id: str) -> None:  # noqa: N802 - QML API
        self.operationRetryRequested.emit(operation_id)

    @Slot(str, float, float, float, float, float, bool)
    def setImageFraming(  # noqa: N802 - QML API
        self,
        item_id: str,
        zoom: float,
        norm_x: float,
        norm_y: float,
        source_width: float,
        source_height: float,
        snap_zoom_to_cover: bool,
    ) -> None:
        self.imageFramingSetRequested.emit(
            item_id,
            zoom,
            norm_x,
            norm_y,
            source_width,
            source_height,
            snap_zoom_to_cover,
        )

    @Slot(str)
    def resetImageFraming(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.imageFramingResetRequested.emit(item_id)

    @Slot(str, float, float, float)
    def setMediaTrim(  # noqa: N802 - QML API
        self,
        item_id: str,
        start_ms: float,
        end_ms: float,
        duration_ms: float,
    ) -> None:
        self.mediaTrimSetRequested.emit(
            item_id,
            start_ms,
            end_ms,
            duration_ms,
        )

    @Slot(result=float)
    def imageFramingAspectRatio(self) -> float:  # noqa: N802 - QML API
        try:
            ratio = self._projection_aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - defensive UI provider boundary
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        return ratio.value

    @Slot(str, result=float)
    def imageFramingSourceAspectRatio(self, item_id: str) -> float:  # noqa: N802
        try:
            ratio = float(self._image_source_aspect_ratio_provider(item_id))
        except Exception:  # noqa: BLE001 - defensive UI provider boundary
            return 0.0
        return ratio if ratio > 0.0 else 0.0

    @Slot(str, str)
    def renameMarker(self, marker_id: str, text: str):
        self.renameMarkerSignal.emit(marker_id, text)

    @Slot(str)
    def deleteMarker(self, marker_id: str):
        self.deleteMarkerSignal.emit(marker_id)

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot(str, int)
    def pointerCursorEnter(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self.pointerCursorEntered.emit(cursor_source, cursor_shape)

    @Slot(str, int)
    def pointerCursorChange(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self.pointerCursorChanged.emit(cursor_source, cursor_shape)

    @Slot(str)
    def pointerCursorExit(self, cursor_source: str):  # noqa: N802
        self.pointerCursorExited.emit(cursor_source)

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()

    @Slot(str)
    def renameSection(self, section_id: str):
        self.renameSectionSignal.emit(section_id)

    @Slot(str)
    def deleteSection(self, section_id: str):
        self.deleteSectionSignal.emit(section_id)

    @Slot(str)
    def recolorSection(self, section_id: str):
        self.recolorSectionSignal.emit(section_id)

    @Slot(str)
    def toggleCollapse(self, section_id: str):
        self.collapseSectionSignal.emit(section_id)

    @Slot(str, str, str, result=bool)
    def canDrop(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        if not self._tree_session:
            return False
        return self._tree_session.can_drop(node_id, node_type, target_list_id)

    @Slot(result=int)
    def treeStructureRevision(self) -> int:  # noqa: N802
        return self._tree_session.structure_revision if self._tree_session else -1

    @Slot(str, str, int, str, int, result=bool)
    def moveNode(
        self,
        node_id: str,
        target_list_id: str,
        insert_index: int,
        expected_tree_id: str,
        expected_structure_revision: int,
    ) -> bool:
        if not self._tree_session:
            return False
        ok = self._tree_session.move(
            node_id,
            target_list_id,
            insert_index,
            expected_tree_id,
            expected_structure_revision,
        )
        if ok:
            self.dragFinished.emit()
        return ok
