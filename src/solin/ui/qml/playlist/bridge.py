"""QML bridge facade for PlaylistEditView.

Architecture
────────────
- PlaylistEditModel owns the row data.
- PlaylistEditBridge exposes playlist actions and state to QML.
- PlaylistThumbnailProvider / PlaylistIconProvider live in playlist.visuals.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Property, Signal, Slot

if TYPE_CHECKING:
    from solin.ui.qml.playlist.model import PlaylistEditModel


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
    exportRequested        = Signal()
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
    renameMarkerSignal   = Signal(str, str)     # marker_id, text
    deleteMarkerSignal   = Signal(str)          # marker_id

    renameSectionSignal  = Signal(str)          # section_id
    deleteSectionSignal  = Signal(str)          # section_id
    recolorSectionSignal = Signal(str)          # section_id

    collapseSectionSignal = Signal(str)         # section_id

    dragFinished = Signal()                     # trigger save after drag
    moveEntrySignal = Signal(int, int)

    # State changed signal for full playlist data refreshes.
    stateChanged = Signal()
    # Lightweight metadata changes that should not rebuild the QML tree.
    chromeChanged = Signal()
    mediaChanged = Signal(str, str, str, str)   # item_id, title, duration, thumb_source
    mediaInserted = Signal(str, int, "QVariant")  # list_id, insert_index, media nodes
    nodesInserted = Signal(str, int, "QVariant")  # list_id, insert_index, tree nodes
    nodeReplaced = Signal(str, "QVariant")      # node_id, replacement tree nodes
    sectionChanged = Signal(str, str, str, str, str, int)
    sectionCountsChanged = Signal("QVariant")
    markerEditRequested = Signal(str)           # marker_id
    cloudChanged = Signal(str, bool, bool, float, str)
    pointerEntered = Signal()
    pointerExited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playlist_name = ""
        self._is_temp = False
        self._is_watched = False
        self._has_items = False
        self._item_count_text = "0 items"
        self._model: PlaylistEditModel | None = None

    def attach_model(self, model: PlaylistEditModel) -> None:
        self._model = model
        self.stateChanged.emit()

    def emit_media_changed(self, item_id: str) -> None:
        if not self._model:
            return
        patch = self._model.media_patch(item_id)
        if not patch:
            return
        self.mediaChanged.emit(
            item_id,
            patch.get("title", ""),
            patch.get("duration", ""),
            patch.get("thumbSource", ""),
        )

    def emit_cloud_changed_for_url(self, url: str) -> None:
        if not self._model:
            return
        for patch in self._model.cloud_patches_for_url(url):
            self.cloudChanged.emit(
                patch.get("id", ""),
                bool(patch.get("cloudVisible", False)),
                bool(patch.get("cloudActive", False)),
                float(patch.get("cloudProgress", -1.0)),
                patch.get("cloudTooltip", ""),
            )

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
    def hasItems(self):
        return self._has_items

    @Property(str, notify=chromeChanged)
    def itemCountText(self):
        return self._item_count_text

    @Property("QVariant", notify=stateChanged)
    def playlistData(self):
        return self._model.tree_data() if self._model else []

    # ── State setters (called from Python) ─────────────────────────────────

    def set_state(self, *, name: str = "", is_temp: bool = False,
                  is_watched: bool = False, item_count: int = 0,
                  item_word: str = "items", has_entries: bool | None = None,
                  emit_data_changed: bool = True) -> None:
        self._playlist_name = name
        self._is_temp = is_temp
        self._is_watched = is_watched
        self._has_items = (item_count > 0 if has_entries is None else has_entries)
        self._item_count_text = f"{item_count} {item_word}"
        self.chromeChanged.emit()
        if emit_data_changed:
            self.stateChanged.emit()

    def emit_media_inserted(self, list_id: str, insert_index: int,
                            item_ids: list[str]) -> None:
        if not self._model:
            return
        nodes = [
            self._model.media_patch(item_id)
            for item_id in item_ids
        ]
        nodes = [node for node in nodes if node]
        if nodes:
            self.mediaInserted.emit(list_id, insert_index, nodes)

    def emit_nodes_inserted(self, list_id: str, insert_index: int,
                            node_ids: list[str]) -> None:
        if not self._model:
            return
        nodes = [
            self._model.node_patch(node_id)
            for node_id in node_ids
        ]
        nodes = [node for node in nodes if node]
        if nodes:
            self.nodesInserted.emit(list_id, insert_index, nodes)

    def emit_node_replaced(self, node_id: str, replacement_ids: list[str]) -> None:
        if not self._model:
            return
        nodes = [
            self._model.node_patch(replacement_id)
            for replacement_id in replacement_ids
        ]
        self.nodeReplaced.emit(node_id, [node for node in nodes if node])

    def emit_section_changed(self, section_id: str) -> None:
        if not self._model:
            return
        patch = self._model.section_patch(section_id)
        if not patch:
            return
        self.sectionChanged.emit(
            section_id,
            patch.get("title", ""),
            patch.get("color", ""),
            patch.get("textColor", ""),
            patch.get("badgeBg", ""),
            int(patch.get("itemCount", 0)),
        )

    def emit_section_counts_changed(self) -> None:
        if not self._model:
            return

        counts: dict[str, int] = {}

        def visit(nodes: list[dict]) -> None:
            for node in nodes:
                if node.get("type") in ("section", "subsection"):
                    counts[node.get("id", "")] = int(node.get("itemCount", 0))
                    visit(node.get("children", []))

        visit(self._model.tree_data())
        if counts:
            self.sectionCountsChanged.emit(counts)

    # ── Slots (called from QML) ────────────────────────────────────────────

    @Slot()
    def backClicked(self):
        self.backRequested.emit()

    @Slot()
    def addClicked(self):
        self.addRequested.emit()

    @Slot()
    def exportClicked(self):
        self.exportRequested.emit()

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

    @Slot(str, str)
    def renameMarker(self, marker_id: str, text: str):
        self.renameMarkerSignal.emit(marker_id, text)

    @Slot(str)
    def deleteMarker(self, marker_id: str):
        self.deleteMarkerSignal.emit(marker_id)

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

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
        if not self._model:
            return False
        return self._model.can_drop_node(node_id, node_type, target_list_id)

    @Slot(str, str, int, result=bool)
    def moveNode(self, node_id: str, target_list_id: str, insert_index: int) -> bool:
        if not self._model:
            return False
        ok = self._model.move_node(node_id, target_list_id, insert_index)
        if ok:
            self.emit_section_counts_changed()
            self.dragFinished.emit()
        return ok

    @Slot(int, int)
    def moveEntry(self, from_index: int, to_index: int):
        """Called by QML during live drag to reorder entries."""
        self.moveEntrySignal.emit(from_index, to_index)

    @Slot()
    def onDragFinished(self):
        """Called by QML when a drag operation ends."""
        self.dragFinished.emit()
