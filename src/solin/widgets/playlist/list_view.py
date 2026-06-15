from __future__ import annotations

import os
from collections.abc import Callable
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.foundation.runtime_paths import ProfilePaths
from ...core.jw.language_context import jw_media_language_context
from ...core.playlists.jwl_files import (
    PlaylistWriteError,
    read_jwlplaylist_document,
    write_jwlplaylist_document,
)
from ...core.playlists.storage import PlaylistStoragePaths
from ...core.playlists.items import create_playlist_item
from ...core.i18n.manager import LanguageManager
from ...styles.icons import ICON_IMPORT, ICON_PLUS, make_icon
from ...core.media.cache import MediaCacheManager
from .components import _CollapsibleSection, _PlaylistCard, _WatchedFolderCard
from .dialogs import NameDialog
from .item_visuals import enrich_items_for_export

if TYPE_CHECKING:
    from ...core.ingest.watched_folder_files import WatchedFolderFileStore
    from ...core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from ...core.media.profile_store import ProfileMediaStore
    from ...core.media.thumbnail_store import ThumbnailStore
    from ...core.playlists.storage import PlaylistRepository

_BTN_STYLE = (
    "QPushButton{border:1px solid #30363d;border-radius:6px;"
    "background:#21262d;padding:0 10px;min-height:28px;"
    "color:#8b949e;font-size:11px;}"
    "QPushButton:hover{background:#2d333b;border-color:#484f58;color:#c9d1d9;}"
    "QPushButton:pressed{background:#161b22;}"
    "QPushButton:disabled{opacity:0.4;}"
)
_BTN_PRIMARY = (
    "QPushButton{border:1px solid #388bfd;border-radius:6px;"
    "background:#1f3a5f;padding:0 10px;min-height:28px;"
    "color:#79c0ff;font-size:11px;font-weight:600;}"
    "QPushButton:hover{background:#2a4f7f;border-color:#58a6ff;color:#cae8ff;}"
    "QPushButton:pressed{background:#163050;}"
    "QPushButton:disabled{opacity:0.4;}"
)


class _PlaylistListView(QWidget):
    open_playlist = Signal(str)
    open_watched_folder = Signal(str)

    _COLS = 3

    def __init__(
        self,
        playlists: list[dict],
        lang: LanguageManager,
        media_ctrl=None,
        watched_folder: str = "",
        *,
        profile_paths: ProfilePaths,
        storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        profile_media_store: ProfileMediaStore,
        playlist_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_playlist_store: WatchedFolderPlaylistStore,
        media_cache_manager: MediaCacheManager,
        schedule_cleanup: Callable[[list[dict]], None],
        parent=None,
    ):
        super().__init__(parent)
        self._playlists = playlists
        self.lang = lang
        self._media_ctrl = media_ctrl
        self._watched_folder = watched_folder
        self._profile_paths = profile_paths
        self._storage_paths = storage_paths
        self._playlist_repository = playlist_repository
        self._profile_media_store = profile_media_store
        self._playlist_thumbnail_store = playlist_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._watched_folder_playlist_store = watched_folder_playlist_store
        self._media_cache_manager = media_cache_manager
        self._schedule_cleanup = schedule_cleanup
        self._pl_cards: list[_PlaylistCard] = []
        self._wf_cards: list[_WatchedFolderCard] = []
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
        self._import_btn.setIcon(make_icon(ICON_IMPORT, 14, "#8b949e"))
        self._import_btn.setIconSize(QSize(14, 14))
        self._import_btn.setText("  " + self.tr("Import"))
        self._import_btn.setStyleSheet(_BTN_STYLE)
        self._import_btn.setFixedHeight(30)
        self._import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_btn.setToolTip(self.tr("Import .jwlplaylist"))
        self._import_btn.clicked.connect(self._import_playlist)
        hdr.addWidget(self._import_btn)

        self._new_btn = QPushButton(self)
        self._new_btn.setIcon(make_icon(ICON_PLUS, 14, "#79c0ff"))
        self._new_btn.setIconSize(QSize(14, 14))
        self._new_btn.setText("  " + self.tr("New Playlist"))
        self._new_btn.setStyleSheet(_BTN_PRIMARY)
        self._new_btn.setFixedHeight(30)
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._create_playlist)
        hdr.addWidget(self._new_btn)
        root.addLayout(hdr)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:#0d1117;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:3px;}"
            "QScrollBar::handle:vertical:hover{background:#484f58;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        self._scroll_wrap = QWidget(self)
        self._scroll_wrap.setStyleSheet("background:transparent;")
        wrap_lay = QVBoxLayout(self._scroll_wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 0)
        wrap_lay.setSpacing(20)

        self._app_section = _CollapsibleSection(self.tr("My Playlists"), parent=self._scroll_wrap)
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
            "color:#484f58;font-size:12px;padding:24px;background:transparent;"
        )
        wrap_lay.addWidget(self._empty_lbl)

        self._wf_section = _CollapsibleSection(self.tr("Link Folder"), parent=self._scroll_wrap)
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
            "color:#484f58;font-size:12px;padding:24px;background:transparent;"
        )
        self._wf_empty_lbl.setVisible(False)
        wrap_lay.addWidget(self._wf_empty_lbl)

        wrap_lay.addStretch(1)
        self._scroll.setWidget(self._scroll_wrap)
        root.addWidget(self._scroll, stretch=1)

    def _rebuild_app_cards(self) -> None:
        while self._app_grid_lay.count():
            it = self._app_grid_lay.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        self._pl_cards.clear()

        has = bool(self._playlists)
        has_wf = bool(self._watched_folder and Path(self._watched_folder).is_dir())
        self._app_section.set_header_visible(has_wf)
        self._empty_lbl.setVisible(not has)
        self._app_grid_cont.setVisible(has)
        self._app_section.set_count(len(self._playlists))

        for i, pl in enumerate(self._playlists):
            card = _PlaylistCard(pl["id"], pl["name"], len(pl.get("items", [])), self.lang)
            card.clicked.connect(self.open_playlist.emit)
            card.rename_req.connect(self._rename_playlist)
            card.delete_req.connect(self._delete_playlist)
            card.export_req.connect(self._export_playlist)
            row, col = divmod(i, self._COLS)
            self._app_grid_lay.addWidget(card, row, col)
            self._pl_cards.append(card)

    def _rebuild_watched_section(self) -> None:
        while self._wf_grid_lay.count():
            it = self._wf_grid_lay.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        self._wf_cards.clear()

        has_root = bool(self._watched_folder and Path(self._watched_folder).is_dir())
        self._wf_section.setVisible(has_root)
        self._wf_empty_lbl.setVisible(False)

        self._app_section.set_header_visible(has_root)
        self._app_grid_cont.setVisible(bool(self._playlists))

        if not has_root:
            self._empty_lbl.setVisible(not bool(self._playlists))
            return

        subfolders = self._watched_folder_playlist_store.scan_root(
            self._watched_folder,
        )
        has_subs = bool(subfolders)
        self._wf_empty_lbl.setVisible(not has_subs)
        self._wf_grid_cont.setVisible(has_subs)
        self._wf_section.set_count(len(subfolders))

        for i, sf in enumerate(subfolders):
            card = _WatchedFolderCard(
                sf["path"], sf["name"], sf["item_count"], self.lang
            )
            card.clicked.connect(self.open_watched_folder.emit)
            card.rename_req.connect(self._rename_watched_folder)
            card.delete_req.connect(self._delete_watched_folder)
            card.export_req.connect(self._export_watched_folder)
            row, col = divmod(i, self._COLS)
            self._wf_grid_lay.addWidget(card, row, col)
            self._wf_cards.append(card)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._title_lbl.setText(self.tr("Playlists"))
        self._import_btn.setText("  " + self.tr("Import"))
        self._import_btn.setToolTip(self.tr("Import .jwlplaylist"))
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

    def refresh_language(self) -> None:
        self.retranslateUi()

    def refresh(self) -> None:
        self._rebuild_app_cards()

    def refresh_watched(self) -> None:
        self._rebuild_watched_section()

    def _create_playlist(self) -> None:
        dlg = NameDialog(lang=self.lang, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        pl = {"id": str(uuid.uuid4()), "name": name, "items": []}
        self._playlists.append(pl)
        self._playlist_repository.save(self._playlists)
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
        name = dlg.get_name()
        if not name:
            return
        pl["name"] = name
        self._playlist_repository.save(self._playlists)
        self._rebuild_app_cards()

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
        self._playlist_repository.save(self._playlists)
        self._schedule_cleanup(list(pl.get("items", [])))
        self._rebuild_app_cards()

    def _export_playlist(self, pl_id: str) -> None:
        pl = self._find(pl_id)
        if not pl:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export .jwlplaylist"),
            os.path.join(
                os.path.expanduser("~"),
                pl["name"].replace(" ", "_") + ".jwlplaylist",
            ),
            "JW Library Playlist (*.jwlplaylist)",
        )
        if not path:
            return
        try:
            items = enrich_items_for_export(
                pl.get("items", []),
                {},
                self._playlist_thumbnail_store,
            )
            fallback_lang = jw_media_language_context(self.lang).fallback_code
            write_jwlplaylist_document(
                pl["name"],
                items,
                path,
                self._media_cache_manager.media_cache_dir,
                fallback_lang_code=fallback_lang,
            )
            QMessageBox.information(
                self,
                self.tr("Export complete"),
                self.tr("Exported:\n{path}").replace("{path}", str(path)),
            )
        except (OSError, ValueError, PlaylistWriteError) as e:
            QMessageBox.critical(self, self.tr("Export error"), str(e))

    def _import_playlist(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("Import .jwlplaylist"),
            os.path.expanduser("~"),
            "JW Library Playlist (*.jwlplaylist)",
        )
        if not paths:
            return
        fallback_lang = jw_media_language_context(self.lang).fallback_code

        imported = 0
        for path in paths:
            try:
                document = read_jwlplaylist_document(
                    path,
                    fallback_lang_code=fallback_lang,
                )
                pl_name = document.name or Path(path).stem
                items = []
                for raw in document.items:
                    url = raw.get("url") or raw.get("jworg_url") or ""
                    item = create_playlist_item(
                        title=raw.get("title", ""),
                        url=url,
                        type=raw.get("type", "video"),
                        key_symbol=raw.get("key_symbol"),
                        track=raw.get("track"),
                        issue_tag=raw.get("issue_tag"),
                        doc_id=raw.get("doc_id"),
                        meps_language=raw.get("language", 0),
                    )
                    if raw.get("data") and not url:
                        item["url"] = self._profile_media_store.save_embedded(
                            raw["data"],
                            raw.get("filename", "media"),
                            identifier=item["id"],
                        )
                        item["type"] = raw.get("type", "video")
                    items.append(item)
                self._playlists.append(
                    {"id": str(uuid.uuid4()), "name": pl_name, "items": items}
                )
                imported += 1
            except (OSError, ValueError) as e:
                QMessageBox.warning(
                    self,
                    self.tr("Import error"),
                    self.tr("Could not import:\n{name}\n\n{error}").replace(
                        "{name}", str(Path(path).name)
                    ).replace("{error}", str(e)),
                )
        if imported:
            self._playlist_repository.save(self._playlists)
            self._rebuild_app_cards()

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
        try:
            self._watched_folder_file_store.rename_folder(folder_path, new_name)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, self.tr("Error"), str(exc))
            return
        self._rebuild_watched_section()

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
        try:
            self._watched_folder_file_store.delete_folder(folder_path)
        except OSError as exc:
            QMessageBox.critical(self, self.tr("Error"), str(exc))
            return
        self._rebuild_watched_section()

    def _export_watched_folder(self, folder_path: str) -> None:
        name = Path(folder_path).name
        pl = self._watched_folder_playlist_store.load_playlist(folder_path)
        items = pl.get("items", [])
        if not items:
            QMessageBox.information(
                self,
                self.tr("Empty folder"),
                self.tr("No media files found in \"{name}\".").replace("{name}", name),
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export .jwlplaylist"),
            os.path.join(os.path.expanduser("~"), name.replace(" ", "_") + ".jwlplaylist"),
            "JW Library Playlist (*.jwlplaylist)",
        )
        if not path:
            return
        try:
            enriched = enrich_items_for_export(
                items,
                {},
                self._playlist_thumbnail_store,
            )
            fallback_lang = jw_media_language_context(self.lang).fallback_code
            write_jwlplaylist_document(
                name,
                enriched,
                path,
                self._media_cache_manager.media_cache_dir,
                fallback_lang_code=fallback_lang,
            )
            QMessageBox.information(
                self,
                self.tr("Export complete"),
                self.tr("Exported:\n{path}").replace("{path}", str(path)),
            )
        except (OSError, ValueError, PlaylistWriteError) as exc:
            QMessageBox.critical(self, self.tr("Export error"), str(exc))
