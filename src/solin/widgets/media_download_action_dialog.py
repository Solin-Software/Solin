from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from solin.styles.theme import PALETTE


class MediaDownloadAction(str, Enum):
    PLAY = "play"
    ADD_TO_PLAYLIST = "add_to_playlist"


class MediaDownloadActionDialog(QDialog):
    """Small decision dialog for media downloads intercepted by the browser."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._action: MediaDownloadAction | None = None

        self.setWindowTitle(self.tr("Media download"))
        self.setModal(True)
        self.setMinimumWidth(330)
        self.setStyleSheet(
            f"QDialog{{background:{PALETTE.surface};border:1px solid {PALETTE.border};border-radius:8px;}}"
            f"QLabel{{color:{PALETTE.text_secondary};font-size:12px;background:transparent;}}"
            f"QPushButton{{border:1px solid {PALETTE.border};border-radius:6px;background:{PALETTE.bg2};"
            f"color:{PALETTE.text_secondary};font-size:12px;padding:7px 12px;}}"
            f"QPushButton:hover{{background:{PALETTE.bg3};border-color:{PALETTE.text_dim};}}"
            f"QPushButton#primary{{border-color:{PALETTE.accent};background:{PALETTE.accent_muted};"
            f"color:{PALETTE.accent_text};font-weight:600;}}"
            f"QPushButton#primary:hover{{background:{PALETTE.accent_muted_hover};color:{PALETTE.accent_text_hover};}}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)

        heading = QLabel(self.tr("What do you want to do with this media?"))
        heading.setWordWrap(True)
        root.addWidget(heading)

        name = QLabel(title)
        name.setWordWrap(True)
        name.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:11px;background:{PALETTE.surface_card};"
            f"border:1px solid {PALETTE.border_muted};border-radius:5px;padding:6px 9px;"
        )
        root.addWidget(name)

        row = QHBoxLayout()
        row.setSpacing(8)

        play_btn = QPushButton(self.tr("Play"))
        play_btn.setObjectName("primary")
        play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        play_btn.clicked.connect(lambda: self._choose(MediaDownloadAction.PLAY))
        row.addWidget(play_btn)

        playlist_btn = QPushButton(self.tr("Add to Playlist"))
        playlist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        playlist_btn.clicked.connect(
            lambda: self._choose(MediaDownloadAction.ADD_TO_PLAYLIST)
        )
        row.addWidget(playlist_btn)

        root.addLayout(row)

        cancel_btn = QPushButton(self.tr("Cancel"))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        root.addWidget(cancel_btn)

    @property
    def action(self) -> MediaDownloadAction | None:
        return self._action

    def _choose(self, action: MediaDownloadAction) -> None:
        self._action = action
        self.accept()


def choose_media_download_action(
    parent,
    title: str,
) -> MediaDownloadAction | None:
    dlg = MediaDownloadActionDialog(title, parent=parent)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.action
    return None
