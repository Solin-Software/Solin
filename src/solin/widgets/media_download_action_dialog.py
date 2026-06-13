from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


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
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
            "QLabel{color:#c9d1d9;font-size:12px;background:transparent;}"
            "QPushButton{border:1px solid #30363d;border-radius:6px;background:#21262d;"
            "color:#c9d1d9;font-size:12px;padding:7px 12px;}"
            "QPushButton:hover{background:#2d333b;border-color:#484f58;}"
            "QPushButton#primary{border-color:#388bfd;background:#1f3a5f;color:#79c0ff;font-weight:600;}"
            "QPushButton#primary:hover{background:#2a4f7f;color:#cae8ff;}"
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
            "color:#8b949e;font-size:11px;background:#13161c;"
            "border:1px solid #21262d;border-radius:5px;padding:6px 9px;"
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
