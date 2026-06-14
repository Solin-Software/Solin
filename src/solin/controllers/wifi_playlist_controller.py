from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.media.formats import AUDIO_EXTS, IMAGE_EXTS, media_type_from_path
from ..core.playlists.items import create_playlist_item


class WifiPlaylistController:
    """Handles Wi-Fi received media playback and playlist actions."""

    _JW_METADATA_KEYS = (
        "key_symbol",
        "track",
        "issue_tag",
        "doc_id",
        "meps_language",
    )

    def __init__(self, window) -> None:
        self._window = window

    def on_wifi_media_received(self, _path: str, _orig_name: str) -> None:
        # The Wi-Fi widget owns received-card state. Users choose follow-up
        # actions through the card buttons, so this signal intentionally does
        # not auto-add anything to playlists.
        return

    def on_wifi_request_play(self, path: str, title: str) -> None:
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS:
            self._window._media_projection.on_cache_play(
                path,
                "image",
                "",
                title,
            )
            return

        media_type = "audio" if ext in self._audio_exts() else "video"
        playlist = [{"url": path, "title": title, "type": media_type}]
        self._window._media_projection.project_video(
            path,
            title,
            playlist,
            None,
            from_saved_playlist=False,
        )

    def on_wifi_request_add_single(
        self,
        path: str,
        title: str,
        orig_name: str,
    ) -> None:
        playlists = self._window.playlist_widget.get_playlist_names()
        entry = self._received_entry(path)

        dlg = QDialog(self._window)
        dlg.setWindowTitle(self._window.tr("Add to Playlist"))
        dlg.setModal(True)
        dlg.setMinimumWidth(340)
        dlg.setMaximumHeight(520)
        self._style_dialog(dlg)

        root = QVBoxLayout(dlg)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        short_title = (title[:52] + "…") if len(title) > 52 else title
        media_lbl = QLabel(f"🎬  {short_title}")
        media_lbl.setStyleSheet(
            "color:#8b949e;font-size:11px;background:#13161c;"
            "border:1px solid #21262d;border-radius:5px;padding:5px 10px;"
        )
        media_lbl.setWordWrap(True)
        root.addWidget(media_lbl)

        def _show_success_notification(message: str):
            dlg.accept()
            self._window.notifications.success(message)
            self._discard_wifi_tmp(path)
            self._window.wifi_receive_widget.remove_received_file(path)

        if playlists:
            self._add_playlist_picker(
                root,
                playlists,
                lambda playlist_id, playlist_name: self._add_single_to_existing_playlist(
                    playlist_id,
                    playlist_name,
                    path,
                    title,
                    orig_name,
                    entry,
                    _show_success_notification,
                ),
            )
        else:
            info = QLabel(self._window.tr("No playlists found.\nCreate a new one:"))
            info.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info.setStyleSheet(
                "color:#484f58;font-size:11px;background:transparent;padding:6px;"
            )
            root.addWidget(info)

        self._add_create_row(
            root,
            self._window.tr("Create and add"),
            lambda name: self._create_playlist_with_single_item(
                name,
                path,
                title,
                orig_name,
                entry,
                _show_success_notification,
            ),
        )
        self._add_close_button(root, dlg)

        dlg.exec()

    def on_wifi_send_all_to_playlist(self, items: list) -> None:
        if not items:
            return

        playlists = self._window.playlist_widget.get_playlist_names()

        dlg = QDialog(self._window)
        dlg.setWindowTitle(self._window.tr("Send Media to Playlist"))
        dlg.setModal(True)
        dlg.setMinimumWidth(360)
        dlg.setMaximumHeight(540)
        self._style_dialog(dlg)

        root = QVBoxLayout(dlg)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        summary = QLabel(
            self._window.tr("📲  %n file(s) received via Wi-Fi", "", len(items))
        )
        summary.setStyleSheet(
            "color:#8b949e;font-size:11px;background:#13161c;"
            "border:1px solid #21262d;border-radius:5px;padding:5px 10px;"
        )
        root.addWidget(summary)

        def _show_success_notification(message: str):
            dlg.accept()
            self._window.notifications.success(message)
            self._window.wifi_receive_widget.clear_all_received()

        if playlists:
            self._add_playlist_picker(
                root,
                playlists,
                lambda playlist_id, playlist_name: self._add_all_to_existing_playlist(
                    playlist_id,
                    playlist_name,
                    items,
                    _show_success_notification,
                ),
            )
        else:
            info = QLabel(self._window.tr("No playlists found.\nCreate a new one:"))
            info.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info.setStyleSheet(
                "color:#484f58;font-size:11px;background:transparent;padding:6px;"
            )
            root.addWidget(info)

        self._add_create_row(
            root,
            self._window.tr("Create and add"),
            lambda name: self._create_playlist_with_all_items(
                name,
                items,
                _show_success_notification,
            ),
        )
        self._add_close_button(root, dlg)

        dlg.exec()

    def item_from_wifi_entry(
        self,
        entry: dict,
        *,
        path: str | None = None,
        title: str | None = None,
        orig_name: str | None = None,
    ) -> dict:
        item_path = path or entry["path"]
        item_title = title or entry["title"]
        media_type = entry.get("type") or media_type_from_path(
            item_path,
            default="video",
        )
        kwargs = self._metadata_kwargs(entry, orig_name)
        return create_playlist_item(
            title=item_title,
            url=item_path,
            type=media_type,
            **kwargs,
        )

    def _metadata_kwargs(self, entry: dict, orig_name: str | None = None) -> dict:
        kwargs = {}
        for key in self._JW_METADATA_KEYS:
            if key in entry and entry[key] is not None:
                kwargs[key] = entry[key]
        original_filename = orig_name if orig_name is not None else entry.get("orig_name", "")
        if original_filename:
            kwargs["original_filename"] = original_filename
        return kwargs

    def _received_entry(self, path: str) -> dict:
        return next(
            (
                entry
                for entry in self._window.wifi_receive_widget._received_files
                if entry.get("path") == path
            ),
            {},
        )

    def _add_single_to_existing_playlist(
        self,
        playlist_id: str,
        playlist_name: str,
        path: str,
        title: str,
        orig_name: str,
        entry: dict,
        on_done,
    ) -> None:
        item = self.item_from_wifi_entry(
            entry,
            path=path,
            title=title,
            orig_name=orig_name,
        )
        added = self._window.playlist_widget.add_item_to_playlist(playlist_id, item)
        if added:
            on_done(
                self._window.tr('Added to playlist\n"%1"').replace(
                    "%1",
                    playlist_name,
                )
            )
        else:
            on_done(
                self._window.tr('This media is already in\nplaylist "%1"').replace(
                    "%1",
                    playlist_name,
                )
            )

    def _create_playlist_with_single_item(
        self,
        name: str,
        path: str,
        title: str,
        orig_name: str,
        entry: dict,
        on_done,
    ) -> None:
        item = self.item_from_wifi_entry(
            entry,
            path=path,
            title=title,
            orig_name=orig_name,
        )
        self._window.playlist_widget.create_playlist_with_item(name, item)
        on_done(
            self._window.tr('Playlist "%1"\ncreated successfully!').replace(
                "%1",
                name,
            )
        )

    def _add_all_to_existing_playlist(
        self,
        playlist_id: str,
        playlist_name: str,
        items: list,
        on_done,
    ) -> None:
        added = 0
        for entry in items:
            item = self.item_from_wifi_entry(entry)
            if self._window.playlist_widget.add_item_to_playlist(playlist_id, item):
                self._discard_wifi_tmp(entry["path"])
                added += 1
        on_done(
            self._window.tr('%1 file(s) added\nto playlist "%2"')
            .replace("%1", str(added))
            .replace("%2", playlist_name)
        )

    def _create_playlist_with_all_items(self, name: str, items: list, on_done) -> None:
        first_item = self.item_from_wifi_entry(items[0])
        playlist_id = self._window.playlist_widget.create_playlist_with_item(
            name,
            first_item,
        )
        self._discard_wifi_tmp(items[0]["path"])
        added = 1
        for entry in items[1:]:
            item = self.item_from_wifi_entry(entry)
            if self._window.playlist_widget.add_item_to_playlist(playlist_id, item):
                self._discard_wifi_tmp(entry["path"])
                added += 1
        on_done(
            self._window.tr('Playlist "%1" created\nwith %2 file(s)!')
            .replace("%1", name)
            .replace("%2", str(added))
        )

    def _discard_wifi_tmp(self, path: str) -> None:
        self._window.wifi_receive_widget._wifi_tmp_files.discard(path)

    def _style_dialog(self, dlg: QDialog) -> None:
        dlg.setStyleSheet(
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
            "QLabel{color:#c9d1d9;font-size:12px;background:transparent;}"
            "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:6px;"
            "color:#e6edf3;font-size:12px;padding:5px 8px;}"
            "QLineEdit:focus{border-color:#388bfd;}"
        )

    def _add_playlist_picker(self, root: QVBoxLayout, playlists: list, callback) -> None:
        label = QLabel(self._window.tr("Select a playlist:"))
        label.setStyleSheet("color:#8b949e;font-size:10px;background:transparent;")
        root.addWidget(label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(200)
        scroll.setStyleSheet(
            "QScrollArea{background:#0d1117;border:1px solid #21262d;border-radius:6px;}"
            "QScrollBar:vertical{background:#0d1117;width:5px;border-radius:2px;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:2px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        for playlist_id, playlist_name in playlists:
            btn = QPushButton(f"  {playlist_name}")
            btn.setFixedHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton{border:none;background:transparent;color:#c9d1d9;"
                "font-size:11px;text-align:left;border-radius:5px;padding:0 10px;}"
                "QPushButton:hover{background:#1f3a5f;color:#79c0ff;border:none;}"
            )
            btn.clicked.connect(
                lambda _checked=False, pid=playlist_id, pname=playlist_name: callback(
                    pid,
                    pname,
                )
            )
            layout.addWidget(btn)

        layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll)

        sep = QLabel(self._window.tr("── or create a new one ──"))
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sep.setStyleSheet("color:#30363d;font-size:10px;background:transparent;")
        root.addWidget(sep)

    def _add_create_row(self, root: QVBoxLayout, button_text: str, callback) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(self._window.tr("New playlist name…"))
        edit.setFixedHeight(30)
        row.addWidget(edit, stretch=1)

        create_btn = QPushButton(button_text)
        create_btn.setFixedHeight(30)
        create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_btn.setStyleSheet(
            "QPushButton{border:1px solid #388bfd;border-radius:6px;"
            "background:#1f3a5f;padding:0 10px;color:#79c0ff;font-size:11px;font-weight:600;}"
            "QPushButton:hover{background:#2a4f7f;}"
        )

        def _create():
            name = edit.text().strip()
            if not name:
                edit.setFocus()
                return
            callback(name)

        create_btn.clicked.connect(_create)
        edit.returnPressed.connect(_create)
        row.addWidget(create_btn)
        root.addLayout(row)
        return edit

    def _add_close_button(self, root: QVBoxLayout, dlg: QDialog) -> None:
        close_btn = QPushButton(self._window.tr("Close"))
        close_btn.setFixedHeight(28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton{border:1px solid #30363d;border-radius:6px;"
            "background:#21262d;color:#8b949e;font-size:11px;}"
            "QPushButton:hover{background:#2d333b;color:#c9d1d9;}"
        )
        close_btn.clicked.connect(dlg.reject)
        root.addWidget(close_btn)

    def _audio_exts(self):
        return getattr(self._window, "_AUDIO_EXTS", AUDIO_EXTS)
