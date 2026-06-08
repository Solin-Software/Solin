from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class PlaylistTarget:
    playlist_id: str
    playlist_name: str
    create_new: bool = False


class PlaylistTargetDialog(QDialog):
    """Reusable chooser for selecting an existing playlist or creating one."""

    def __init__(
        self,
        media_title: str,
        playlists: list[tuple[str, str]],
        parent=None,
        window_title: str | None = None,
    ):
        super().__init__(parent)
        self._target: PlaylistTarget | None = None

        self.setWindowTitle(window_title or self.tr("Add to Playlist"))
        self.setModal(True)
        self.setMinimumWidth(340)
        self.setMaximumHeight(520)
        self.setStyleSheet(
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
            "QLabel{color:#c9d1d9;font-size:12px;background:transparent;}"
            "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:6px;"
            "color:#e6edf3;font-size:12px;padding:5px 8px;}"
            "QLineEdit:focus{border-color:#388bfd;}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        short_title = (media_title[:52] + "...") if len(media_title) > 52 else media_title
        media_lbl = QLabel(short_title)
        media_lbl.setStyleSheet(
            "color:#8b949e;font-size:11px;background:#13161c;"
            "border:1px solid #21262d;border-radius:5px;padding:5px 10px;"
        )
        media_lbl.setWordWrap(True)
        root.addWidget(media_lbl)

        if playlists:
            lbl = QLabel(self.tr("Select a playlist:"))
            lbl.setStyleSheet("color:#8b949e;font-size:10px;background:transparent;")
            root.addWidget(lbl)

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
            vl = QVBoxLayout(container)
            vl.setContentsMargins(4, 4, 4, 4)
            vl.setSpacing(2)

            for pl_id, pl_name in playlists:
                btn = QPushButton(f"  {pl_name}")
                btn.setFixedHeight(34)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton{border:none;background:transparent;color:#c9d1d9;"
                    "font-size:11px;text-align:left;border-radius:5px;padding:0 10px;}"
                    "QPushButton:hover{background:#1f3a5f;color:#79c0ff;border:none;}"
                )
                btn.clicked.connect(
                    lambda checked=False, pid=pl_id, name=pl_name: self._select_existing(pid, name)
                )
                vl.addWidget(btn)

            vl.addStretch()
            scroll.setWidget(container)
            root.addWidget(scroll)

            sep = QLabel(self.tr("-- or create a new one --"))
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#30363d;font-size:10px;background:transparent;")
            root.addWidget(sep)
        else:
            info = QLabel(self.tr("No playlists found.\nCreate a new one:"))
            info.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info.setStyleSheet("color:#484f58;font-size:11px;background:transparent;padding:6px;")
            root.addWidget(info)

        new_row = QHBoxLayout()
        self._new_edit = QLineEdit()
        self._new_edit.setPlaceholderText(self.tr("New playlist name..."))
        self._new_edit.setFixedHeight(30)
        new_row.addWidget(self._new_edit, stretch=1)

        create_btn = QPushButton(self.tr("Create and add"))
        create_btn.setFixedHeight(30)
        create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_btn.setStyleSheet(
            "QPushButton{border:1px solid #388bfd;border-radius:6px;"
            "background:#1f3a5f;padding:0 10px;color:#79c0ff;font-size:11px;font-weight:600;}"
            "QPushButton:hover{background:#2a4f7f;}"
        )
        create_btn.clicked.connect(self._select_new)
        self._new_edit.returnPressed.connect(self._select_new)
        new_row.addWidget(create_btn)
        root.addLayout(new_row)

        close_btn = QPushButton(self.tr("Close"))
        close_btn.setFixedHeight(28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton{border:1px solid #30363d;border-radius:6px;"
            "background:#21262d;color:#8b949e;font-size:11px;}"
            "QPushButton:hover{background:#2d333b;color:#c9d1d9;}"
        )
        close_btn.clicked.connect(self.reject)
        root.addWidget(close_btn)

    @property
    def target(self) -> PlaylistTarget | None:
        return self._target

    def _select_existing(self, playlist_id: str, playlist_name: str) -> None:
        self._target = PlaylistTarget(playlist_id, playlist_name, False)
        self.accept()

    def _select_new(self) -> None:
        name = self._new_edit.text().strip()
        if not name:
            self._new_edit.setFocus()
            return
        self._target = PlaylistTarget("", name, True)
        self.accept()


def choose_playlist_target(
    parent,
    media_title: str,
    playlists: list[tuple[str, str]],
    window_title: str | None = None,
) -> PlaylistTarget | None:
    dlg = PlaylistTargetDialog(
        media_title=media_title,
        playlists=playlists,
        parent=parent,
        window_title=window_title,
    )
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.target
    return None
