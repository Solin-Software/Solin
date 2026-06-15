from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ...styles.icons import ICON_FOLDER_LINK, make_icon
from ._shared import (
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DANGER,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_TEXT,
)


class WatchedFolderSectionMixin:
    """Builds and manages the linked-folder settings section."""

    def _build_watched_folder_card(self):
        card, lay = self._card()
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(14, 10, 14, 10)
        row_lay.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(make_icon(ICON_FOLDER_LINK, size=18, color=SETTINGS_MUTED).pixmap(18, 18))
        icon_lbl.setFixedSize(20, 20)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("background: transparent; border: none;")
        row_lay.addWidget(icon_lbl)

        col = QVBoxLayout()
        col.setSpacing(2)
        self._watched_folder_title_lbl = QLabel(self.tr("Link Folder"))
        self._watched_folder_title_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        col.addWidget(self._watched_folder_title_lbl)
        self._watched_folder_desc_lbl = QLabel(
            self.tr("Sync folder (Dropbox, OneDrive, etc.) shown as playlists.")
        )
        self._watched_folder_desc_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._watched_folder_desc_lbl.setWordWrap(True)
        col.addWidget(self._watched_folder_desc_lbl)
        saved = self._watched_folder_settings.path()
        self._watched_folder_path_lbl = QLabel()
        self._watched_folder_path_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_MUTED}; background: transparent; border: none;"
        )
        self._watched_folder_path_lbl.setWordWrap(False)
        self._sync_watched_folder_path_label(saved)
        col.addWidget(self._watched_folder_path_lbl)
        row_lay.addLayout(col, stretch=1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        self._watched_folder_pick_btn = QPushButton(self.tr("Choose\u2026"))
        self._watched_folder_pick_btn.setFixedHeight(28)
        self._watched_folder_pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._watched_folder_pick_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 6px;"
            f" background: {SETTINGS_BORDER}; color: #c9d1d9; font-size: 11px; padding: 0 10px; }}"
            f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; }}"
        )
        self._watched_folder_pick_btn.clicked.connect(self._pick_watched_folder)
        btn_col.addWidget(self._watched_folder_pick_btn)

        self._watched_folder_clear_btn = QPushButton(self.tr("Clear"))
        self._watched_folder_clear_btn.setFixedHeight(28)
        self._watched_folder_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._watched_folder_clear_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 6px;"
            f" background: {SETTINGS_BORDER}; color: {SETTINGS_MUTED}; font-size: 11px; padding: 0 10px; }}"
            f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; color: {SETTINGS_DANGER};"
            f" border-color: {SETTINGS_DANGER}; }}"
        )
        self._watched_folder_clear_btn.clicked.connect(self._clear_watched_folder)
        btn_col.addWidget(self._watched_folder_clear_btn)
        row_lay.addLayout(btn_col)

        lay.addWidget(row)
        self._watched_folder_clear_btn.setVisible(bool(saved))
        return card

    def _sync_watched_folder_path_label(self, path: str | None = None) -> None:
        if path is None:
            path = self._watched_folder_settings.path()
        self._watched_folder_path_lbl.setText(path or self.tr("No folder selected"))

    def _pick_watched_folder(self):
        current = self._watched_folder_settings.path()
        start = current if current else os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, self.tr("Select folder to link"), start
        )
        if not path:
            return
        self._watched_folder_settings.set_path(path)
        self._sync_watched_folder_path_label(path)
        self._watched_folder_clear_btn.setVisible(True)
        self.watched_folder_changed.emit(path)

    def _clear_watched_folder(self):
        self._watched_folder_settings.clear_path()
        self._sync_watched_folder_path_label("")
        self._watched_folder_clear_btn.setVisible(False)
        self.watched_folder_changed.emit("")

    def get_watched_folder(self):
        return self._watched_folder_settings.path()
