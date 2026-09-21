from __future__ import annotations

import os
from collections.abc import Callable
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import LanguageManager
from ...core.foundation.resource_keys import (
    folder_read_resource_claim,
    folder_resource_key,
)
from ...core.media.operations import (
    MediaOperationPresentation,
    MediaOperationSpec,
)
from ...core.playlists.names import (
    PlaylistNameConflictError,
    PlaylistNameError,
    ensure_unique_playlist_name,
)
from ...styles.icons import ICON_IMPORT, ICON_PLUS, make_icon
from ...styles.theme import PALETTE
from .components import (
    CollapsibleSection,
    PlaylistCard,
    WatchedFolderCard,
    playlist_card_menu_stylesheet,
)
from .dialogs import NameDialog

if TYPE_CHECKING:
    from ...core.ingest.watched_folder_files import WatchedFolderFileStore
    from ...core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore

__all__ = (
    "PLAYLIST_PRIMARY_BUTTON_STYLESHEET",
    "PLAYLIST_SECONDARY_BUTTON_STYLESHEET",
    "PlaylistListView",
)

def playlist_secondary_button_stylesheet() -> str:
    return (
        f"QPushButton{{border:1px solid {PALETTE.border};border-radius:6px;"
        f"background:{PALETTE.bg2};padding:0 10px;min-height:28px;"
        f"color:{PALETTE.text_muted};font-size:11px;}}"
        f"QPushButton:hover{{background:{PALETTE.bg3};border-color:{PALETTE.text_dim};"
        f"color:{PALETTE.text_secondary};}}"
        f"QPushButton:pressed{{background:{PALETTE.bg1};}}"
        "QPushButton:disabled{opacity:0.4;}"
    )


def playlist_primary_button_stylesheet() -> str:
    return (
        f"QPushButton{{border:1px solid {PALETTE.accent};border-radius:6px;"
        f"background:{PALETTE.accent_muted};padding:0 10px;min-height:28px;"
        f"color:{PALETTE.accent_text};font-size:11px;font-weight:600;}}"
        f"QPushButton:hover{{background:{PALETTE.accent_muted_hover};"
        f"border-color:{PALETTE.accent_hover};color:{PALETTE.accent_text_hover};}}"
        f"QPushButton:pressed{{background:{PALETTE.accent_tint};}}"
        "QPushButton:disabled{opacity:0.4;}"
    )


PLAYLIST_SECONDARY_BUTTON_STYLESHEET = playlist_secondary_button_stylesheet()
PLAYLIST_PRIMARY_BUTTON_STYLESHEET = playlist_primary_button_stylesheet()


class PlaylistListView(QWidget):
    open_playlist = Signal(str)
    open_watched_folder = Signal(str)
    import_requested = Signal(object, str)  # paths, format
    export_playlist_requested = Signal(str, str)  # playlist_id, format
    export_watched_folder_requested = Signal(str, str)  # folder_path, format

    _COLS = 3

    def __init__(
        self,
        playlists: list[dict],
        lang: LanguageManager,
        media_ctrl=None,
        watched_folder: str = "",
        *,
        persist_playlists: Callable[[], None],
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_playlist_store: WatchedFolderPlaylistStore,
        media_tree_runtime,
        schedule_cleanup: Callable[[list[dict]], None],
        parent=None,
    ):
        super().__init__(parent)
        self._playlists = playlists
        self.lang = lang
        self._media_ctrl = media_ctrl
        self._watched_folder = watched_folder
        self._persist_playlists = persist_playlists
        self._watched_folder_file_store = watched_folder_file_store
        self._watched_folder_playlist_store = watched_folder_playlist_store
        self._media_tree_runtime = media_tree_runtime
        self._schedule_cleanup = schedule_cleanup
        self._pl_cards: list[PlaylistCard] = []
        self._wf_cards: list[WatchedFolderCard] = []
        self._watched_scan_generation = 0
        self._watched_scan_operation_id = ""
        self._watched_mutation_operation_ids: set[str] = set()
        self._watched_mutation_paths: set[str] = set()
        self._cleaning_up = False
        self._build_ui()

    def set_watched_folder(self, path: str) -> None:
        self._watched_folder = path
        self._rebuild_watched_section()
    def showEvent(self, e) -> None:
        self._rebuild_app_cards()
        self._rebuild_watched_section()
        super().showEvent(e)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        hdr = QHBoxLayout()
        self._title_lbl = QLabel(self.tr("Playlists"), self)
        self._title_lbl.setObjectName("SectionTitle")
        hdr.addWidget(self._title_lbl)
        hdr.addStretch()

        self._import_btn = QPushButton(self)
        self._import_btn.setIcon(make_icon(ICON_IMPORT, 14, PALETTE.text_muted))
        self._import_btn.setIconSize(QSize(14, 14))
        self._import_btn.setText("  " + self.tr("Import"))
        self._import_btn.setStyleSheet(playlist_secondary_button_stylesheet())
        self._import_btn.setFixedHeight(30)
        self._import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_btn.setToolTip(self.tr("Import playlist"))
        self._import_btn.clicked.connect(self._show_import_menu)
        hdr.addWidget(self._import_btn)

        self._new_btn = QPushButton(self)
        self._new_btn.setIcon(make_icon(ICON_PLUS, 14, PALETTE.accent_text))
        self._new_btn.setIconSize(QSize(14, 14))
        self._new_btn.setText("  " + self.tr("New Playlist"))
        self._new_btn.setStyleSheet(playlist_primary_button_stylesheet())
        self._new_btn.setFixedHeight(30)
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._create_playlist)
        hdr.addWidget(self._new_btn)
        root.addLayout(hdr)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._apply_scroll_style()
        self._scroll_wrap = QWidget(self)
        self._scroll_wrap.setStyleSheet("background:transparent;")

        wrap_lay = QVBoxLayout(self._scroll_wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 0)
        wrap_lay.setSpacing(20)

        self._app_section = CollapsibleSection(self.tr("My Playlists"), parent=self._scroll_wrap)
        self._app_grid_cont = QWidget(self._scroll_wrap)
        self._app_grid_cont.setStyleSheet("background:transparent;")
        self._app_grid_lay = QGridLayout(self._app_grid_cont)
        self._app_grid_lay.setContentsMargins(0, 0, 0, 0)
        self._app_grid_lay.setHorizontalSpacing(8)
        self._app_grid_lay.setVerticalSpacing(8)
        for c in range(self._COLS):
            self._app_grid_lay.setColumnStretch(c, 1)
        self._app_section.set_content(self._app_grid_cont)
        wrap_lay.addWidget(self._app_section)

        self._empty_lbl = QLabel(
            self.tr("No playlists yet.\nClick '＋ New Playlist' to create one."),
            self._scroll_wrap,
        )
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:12px;padding:24px;background:transparent;"
        )
        wrap_lay.addWidget(self._empty_lbl)

        self._wf_section = CollapsibleSection(self.tr("Link Folder"), parent=self._scroll_wrap)
        self._wf_grid_cont = QWidget(self._scroll_wrap)
        self._wf_grid_cont.setStyleSheet("background:transparent;")
        self._wf_grid_lay = QGridLayout(self._wf_grid_cont)
        self._wf_grid_lay.setContentsMargins(0, 0, 0, 0)
        self._wf_grid_lay.setHorizontalSpacing(8)
        self._wf_grid_lay.setVerticalSpacing(8)
        for c in range(self._COLS):
            self._wf_grid_lay.setColumnStretch(c, 1)
        self._wf_section.set_content(self._wf_grid_cont)
        self._wf_section.setVisible(False)
        wrap_lay.addWidget(self._wf_section)

        self._wf_empty_lbl = QLabel(
            self.tr("No subfolders found.\nCreate subfolders inside the linked folder to use as playlists."),
            self._scroll_wrap,
        )
        self._wf_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wf_empty_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:12px;padding:24px;background:transparent;"
        )
        self._wf_empty_lbl.setVisible(False)
        wrap_lay.addWidget(self._wf_empty_lbl)

        wrap_lay.addStretch(1)
        self._scroll.setWidget(self._scroll_wrap)
        root.addWidget(self._scroll, stretch=1)

    def _apply_scroll_style(self) -> None:
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            f"QScrollBar:vertical{{background:{PALETTE.bg0};width:6px;border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{PALETTE.border};border-radius:3px;}}"
            f"QScrollBar::handle:vertical:hover{{background:{PALETTE.text_dim};}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

    def apply_theme(self) -> None:
        self._import_btn.setIcon(make_icon(ICON_IMPORT, 14, PALETTE.text_muted))
        self._import_btn.setStyleSheet(playlist_secondary_button_stylesheet())
        self._new_btn.setIcon(make_icon(ICON_PLUS, 14, PALETTE.accent_text))
        self._new_btn.setStyleSheet(playlist_primary_button_stylesheet())
        self._apply_scroll_style()
        self._empty_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:12px;padding:24px;background:transparent;"
        )
        self._wf_empty_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:12px;padding:24px;background:transparent;"
        )
        for section in (self._app_section, self._wf_section):
            section.apply_theme()
        for card in (*self._pl_cards, *self._wf_cards):
            card.apply_theme()

    def _rebuild_app_cards(self) -> None:
        while self._app_grid_lay.count():
            it = self._app_grid_lay.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        self._pl_cards.clear()

        has = bool(self._playlists)
        has_wf = bool(self._watched_folder)
        self._app_section.set_header_visible(has_wf)
        self._empty_lbl.setVisible(not has)
        self._app_grid_cont.setVisible(has)
        self._app_section.set_count(len(self._playlists))

        for i, pl in enumerate(self._playlists):
            card = PlaylistCard(pl["id"], pl["name"], len(pl.get("items", [])), self.lang)
            card.clicked.connect(self.open_playlist.emit)
            card.rename_req.connect(self._rename_playlist)
            card.delete_req.connect(self._delete_playlist)
            card.export_req.connect(self.export_playlist_requested.emit)
            row, col = divmod(i, self._COLS)
            self._app_grid_lay.addWidget(card, row, col)
            self._pl_cards.append(card)

    def _rebuild_watched_section(self) -> None:
        has_root = bool(self._watched_folder)
        self._wf_section.setVisible(has_root)
        self._app_section.set_header_visible(has_root)
        self._app_grid_cont.setVisible(bool(self._playlists))
        if not has_root:
            self._watched_scan_generation += 1
            if self._watched_scan_operation_id:
                self._media_tree_runtime.operations.cancel(
                    self._watched_scan_operation_id
                )
                self._watched_scan_operation_id = ""
            self._render_watched_section([])
            self._empty_lbl.setVisible(not bool(self._playlists))
            return
        self._request_watched_section_snapshot()

    def _request_watched_section_snapshot(self) -> None:
        if self._cleaning_up:
            return
        self._watched_scan_generation += 1
        generation = self._watched_scan_generation
        folder_path = self._watched_folder
        if self._watched_scan_operation_id:
            self._media_tree_runtime.operations.cancel(self._watched_scan_operation_id)
        operation_id = f"playlist-catalog:{uuid.uuid4().hex}"
        self._watched_scan_operation_id = operation_id

        def run(_progress, _cancellation):
            return self._watched_folder_playlist_store.scan_root(folder_path)

        def commit(value: object) -> None:
            if (
                generation != self._watched_scan_generation
                or folder_path != self._watched_folder
            ):
                return
            self._watched_scan_operation_id = ""
            if not isinstance(value, list):
                raise TypeError("Linked-folder catalog returned an invalid result")
            self._render_watched_section(value)

        def finished_without_result(*_args) -> None:
            if generation == self._watched_scan_generation:
                self._watched_scan_operation_id = ""

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id="playlist-catalog",
                operation_type="linked_folder_catalog_scan",
                conflict_key=folder_read_resource_claim(folder_path),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=commit,
                priority=-50,
                failed=finished_without_result,
                cancelled=finished_without_result,
            )
        )
        if not submitted:
            finished_without_result()

    def _render_watched_section(self, subfolders: list[dict]) -> None:
        while self._wf_grid_lay.count():
            it = self._wf_grid_lay.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        self._wf_cards.clear()
        has_subs = bool(subfolders)
        self._wf_empty_lbl.setVisible(not has_subs)
        self._wf_grid_cont.setVisible(has_subs)
        self._wf_section.set_count(len(subfolders))

        for i, sf in enumerate(subfolders):
            card = WatchedFolderCard(
                sf["path"], sf["name"], sf["item_count"], self.lang
            )
            card.clicked.connect(self.open_watched_folder.emit)
            card.rename_req.connect(self._rename_watched_folder)
            card.delete_req.connect(self._delete_watched_folder)
            card.reset_sync_req.connect(self._reset_watched_folder_sync)
            card.export_req.connect(self.export_watched_folder_requested.emit)
            card.setEnabled(
                os.path.normcase(os.path.abspath(sf["path"]))
                not in self._watched_mutation_paths
            )
            row, col = divmod(i, self._COLS)
            self._wf_grid_lay.addWidget(card, row, col)
            self._wf_cards.append(card)

    def cleanup(self) -> None:
        self._cleaning_up = True
        self._watched_scan_generation += 1
        if self._watched_scan_operation_id:
            self._media_tree_runtime.operations.cancel(self._watched_scan_operation_id)
            self._watched_scan_operation_id = ""
        for operation_id in tuple(self._watched_mutation_operation_ids):
            self._media_tree_runtime.operations.cancel(operation_id)
        self._watched_mutation_operation_ids.clear()
        self._watched_mutation_paths.clear()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._title_lbl.setText(self.tr("Playlists"))
        self._import_btn.setText("  " + self.tr("Import"))
        self._import_btn.setToolTip(self.tr("Import playlist"))
        self._new_btn.setText("  " + self.tr("New Playlist"))
        self._empty_lbl.setText(
            self.tr("No playlists yet.\nClick '＋ New Playlist' to create one.")
        )
        self._app_section.set_title(self.tr("My Playlists"))
        self._wf_section.set_title(self.tr("Link Folder"))
        self._wf_empty_lbl.setText(
            self.tr("No subfolders found.\nCreate subfolders inside the linked folder to use as playlists.")
        )
        self._rebuild_app_cards()
        self._rebuild_watched_section()

    def refresh(self) -> None:
        self._rebuild_app_cards()

    def refresh_watched(self) -> None:
        self._rebuild_watched_section()

    def _create_playlist(self) -> None:
        dlg = NameDialog(lang=self.lang, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            name = ensure_unique_playlist_name(dlg.get_name(), self._playlists)
        except PlaylistNameConflictError as exc:
            self._show_duplicate_name_warning(exc.name)
            return
        except PlaylistNameError:
            return
        pl = {"id": str(uuid.uuid4()), "name": name, "items": []}
        self._playlists.append(pl)
        self._persist_playlists()
        self._rebuild_app_cards()
        self.open_playlist.emit(pl["id"])

    def _rename_playlist(self, pl_id: str) -> None:
        pl = self._find(pl_id)
        if not pl:
            return
        dlg = NameDialog(pl["name"], lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Rename playlist"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            name = ensure_unique_playlist_name(
                dlg.get_name(),
                self._playlists,
                excluding_id=pl_id,
            )
        except PlaylistNameConflictError as exc:
            self._show_duplicate_name_warning(exc.name)
            return
        except PlaylistNameError:
            return
        pl["name"] = name
        self._persist_playlists()
        self._rebuild_app_cards()

    def _show_duplicate_name_warning(self, name: str) -> None:
        QMessageBox.warning(
            self,
            self.tr("Playlist already exists"),
            self.tr('A playlist named "{name}" already exists.').replace(
                "{name}", name
            ),
        )

    def _delete_playlist(self, pl_id: str) -> None:
        pl = self._find(pl_id)
        if not pl:
            return
        if self._media_ctrl is not None:
            cur_url = self._media_ctrl.current_url or ""
            cur_local = self._media_ctrl.local_path or ""
            for it in pl.get("items", []):
                item_url = it.get("url", "")
                if item_url and it.get("type", "video") in ("video", "audio"):
                    if item_url in (cur_url, cur_local) or (
                        cur_local and os.path.normpath(item_url) == os.path.normpath(cur_local)
                    ):
                        QMessageBox.warning(
                            self,
                            self.tr("Media is playing"),
                            self.tr(
                                "Cannot delete playlist \"{name}\" because one of its items is currently playing.\nStop the projection and try again."
                            ).replace("{name}", str(pl["name"])),
                        )
                        return
        reply = QMessageBox.question(
            self,
            self.tr("Delete Playlist"),
            self.tr("Delete playlist \"{name}\"?\nThis action cannot be undone.").replace(
                "{name}", str(pl["name"])
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._playlists.remove(pl)
        self._persist_playlists()
        self._schedule_cleanup(list(pl.get("items", [])))
        self._rebuild_app_cards()

    def _show_import_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(playlist_card_menu_stylesheet())

        native_import = QAction(self.tr("Solin Playlist…"), menu)
        native_import.triggered.connect(lambda: self._pick_import("solin"))
        jwl_import = QAction(self.tr("JW Library Playlist…"), menu)
        jwl_import.triggered.connect(lambda: self._pick_import("jwl"))
        menu.addAction(native_import)
        menu.addAction(jwl_import)
        menu.exec(self._import_btn.mapToGlobal(self._import_btn.rect().bottomLeft()))

    def _pick_import(self, playlist_format: str) -> None:
        if playlist_format == "solin":
            title = self.tr("Import Solin Playlist")
            file_filter = "Solin Playlist (*.solinplaylist)"
        else:
            title = self.tr("Import JW Library Playlist")
            file_filter = "JW Library Playlist (*.jwlplaylist)"
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            title,
            os.path.expanduser("~"),
            file_filter,
        )
        if paths:
            self.import_requested.emit(paths, playlist_format)

    def _find(self, pl_id: str) -> Optional[dict]:
        return next((p for p in self._playlists if p["id"] == pl_id), None)

    def _rename_watched_folder(self, folder_path: str) -> None:
        current_name = Path(folder_path).name
        dlg = NameDialog(current_name, lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Rename folder"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dlg.get_name()
        if not new_name or new_name == current_name:
            return
        self._submit_watched_folder_mutation(
            folder_path,
            "rename_linked_playlist_folder",
            lambda: self._watched_folder_playlist_store.rename_folder(
                folder_path,
                new_name,
                self._watched_folder_file_store,
            ),
        )

    def _delete_watched_folder(self, folder_path: str) -> None:
        name = Path(folder_path).name
        reply = QMessageBox.question(
            self,
            self.tr("Delete folder"),
            self.tr(
                "Delete folder \"{name}\" and all its contents from disk?\n"
                "This action cannot be undone."
            ).replace("{name}", name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._submit_watched_folder_mutation(
            folder_path,
            "delete_linked_playlist_folder",
            lambda: self._watched_folder_playlist_store.delete_folder(
                folder_path, self._watched_folder_file_store,
            ),
        )

    def _reset_watched_folder_sync(self, folder_path: str) -> None:
        reply = QMessageBox.question(
            self,
            self.tr("Reset synchronization"),
            self.tr(
                'Reset synchronization for "{name}"?\n'
                "The current organization and files will be preserved. "
                "Pending changes from the previous synchronization will not be applied."
            ).replace("{name}", Path(folder_path).name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._submit_watched_folder_mutation(
            folder_path,
            "reset_linked_playlist_sync",
            lambda: self._watched_folder_playlist_store.reset_sync(folder_path),
        )

    def _submit_watched_folder_mutation(
        self,
        folder_path: str,
        operation_type: str,
        mutate: Callable[[], object],
    ) -> None:
        path_key = os.path.normcase(os.path.abspath(folder_path))
        if path_key in self._watched_mutation_paths:
            return
        operation_id = f"{operation_type}:{uuid.uuid4().hex}"
        self._watched_mutation_paths.add(path_key)
        self._watched_mutation_operation_ids.add(operation_id)
        self._set_watched_card_enabled(path_key, False)

        def run(_progress, _cancellation):
            return mutate()

        def finish() -> None:
            self._watched_mutation_operation_ids.discard(operation_id)
            self._watched_mutation_paths.discard(path_key)
            if not self._cleaning_up:
                self._rebuild_watched_section()

        def commit(_value: object) -> None:
            finish()

        def failed(message: str, _retryable: bool) -> None:
            finish()
            QMessageBox.critical(self, self.tr("Error"), message)

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id="playlist-catalog",
                operation_type=operation_type,
                conflict_key=folder_resource_key(self._watched_folder),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=commit,
                priority=50,
                failed=failed,
                cancelled=finish,
            )
        )
        if not submitted:
            finish()

    def _set_watched_card_enabled(self, path_key: str, enabled: bool) -> None:
        for card in self._wf_cards:
            if os.path.normcase(os.path.abspath(card.path)) == path_key:
                card.setEnabled(enabled)
                return
