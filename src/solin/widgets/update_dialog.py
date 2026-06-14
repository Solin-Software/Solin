"""
update_dialog.py
================
Update dialogs for Solin.

Two modes:
  • SETUP  → informs the user of the new version and opens the browser.
             Nothing is downloaded — the user installs manually.
  • PATCH  → asks, downloads the .exe with a progress bar,
             saves to temp, launches the installer and exits the app.

Temp-file cleanup:
  Before launching the patch, delegates persistence to the injected adapter.
  On next launch, bootstrap delegates deletion to the same remote adapter.
  (Cannot delete while patch.exe is running on Windows.)
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    Qt, QUrl, QObject,
    QPropertyAnimation, QEasingCurve, QByteArray, QPoint,
)
from PySide6.QtGui import QDesktopServices, QMouseEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QFrame,
    QGraphicsOpacityEffect,
)

if TYPE_CHECKING:
    from solin.core.remote.patch_installer import PatchDownloadWorker
    from solin.core.remote.update_policy import UpdateInfo

log = logging.getLogger(__name__)

# ── Palette (mirrors theme.py without circular import) ────────────────────────
_C = {
    "bg0":    "#0d1117",
    "bg1":    "#161b22",
    "bg2":    "#21262d",
    "border": "#30363d",
    "text":   "#e6edf3",
    "muted":  "#8b949e",
    "accent": "#388bfd",
    "green":  "#3fb950",
    "yellow": "#d29922",
    "red":    "#f85149",
}

# The QDialog itself must be transparent (WA_TranslucentBackground is set).
# Only QFrame#card carries the actual background color — this prevents the
# background mismatch caused by the OS compositing the dialog's own bg
# on top of the card's bg.
_QSS = f"""
QDialog {{
    background: transparent;
}}
QFrame#card {{
    background: {_C['bg1']};
    border: 1px solid {_C['border']};
    border-radius: 10px;
}}
QLabel {{
    background: transparent;
}}
QLabel#title {{
    color: {_C['text']};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#body {{
    color: {_C['muted']};
    font-size: 12px;
}}
QLabel#version_badge {{
    color: {_C['green']};
    font-size: 11px;
    font-weight: 600;
    background: rgba(63,185,80,0.12);
    border: 1px solid rgba(63,185,80,0.3);
    border-radius: 4px;
    padding: 2px 8px;
}}
QPushButton#btn_primary {{
    background: {_C['accent']};
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-size: 12px;
    font-weight: 600;
    min-width: 100px;
}}
QPushButton#btn_primary:hover   {{ background: #4d9fff; }}
QPushButton#btn_primary:pressed {{ background: #2c7de0; }}
QPushButton#btn_secondary {{
    background: transparent;
    color: {_C['muted']};
    border: 1px solid {_C['border']};
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 12px;
    min-width: 80px;
}}
QPushButton#btn_secondary:hover {{ color: {_C['text']}; border-color: {_C['muted']}; }}
QProgressBar {{
    background: {_C['bg2']};
    border: 1px solid {_C['border']};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {_C['accent']};
    border-radius: 3px;
}}
"""

# ── Update dialog ─────────────────────────────────────────────────────────────

class UpdateDialog(QDialog):
    """
    Non-modal dialog (show) for SETUP or implicit-modal for PATCH.

    For PATCH, call show() — the dialog manages its own lifecycle,
    exiting the app when the patch is ready.

    The dialog is frameless and draggable: click-and-drag anywhere on it
    to reposition it on screen.
    """

    def __init__(
        self,
        info: "UpdateInfo",
        parent=None,
        *,
        patch_downloader_factory: Callable[[str, QObject], PatchDownloadWorker],
        save_cleanup_path: Callable[[str], None],
        launch_patch: Callable[[str], None],
    ) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self._info       = info
        self._patch_downloader_factory = patch_downloader_factory
        self._save_cleanup_path = save_cleanup_path
        self._launch_patch = launch_patch
        self._downloader: PatchDownloadWorker | None = None
        self._patch_path = ""

        # Drag state
        self._drag_active = False
        self._drag_start_pos = QPoint()

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False)
        self.setMinimumWidth(400)

        self._build_ui()
        self.setStyleSheet(_QSS)
        self._fade_in()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        from solin.core.remote.update_policy import UpdateKind

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("card")
        root.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)

        # ── Header ─────────────────────────────────────────────────────────────
        hdr = QHBoxLayout()

        ico = QLabel("⬆")
        ico.setStyleSheet(f"font-size:22px; color:{_C['accent']}; background:transparent;")
        hdr.addWidget(ico)

        title = QLabel(self.tr("Update available"))
        title.setObjectName("title")
        hdr.addWidget(title)
        hdr.addStretch()

        badge = QLabel(f"v{self._info.version}")
        badge.setObjectName("version_badge")
        hdr.addWidget(badge)
        lay.addLayout(hdr)

        # ── Separator ──────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{_C['border']}; border:none; max-height:1px;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        # ── Body ───────────────────────────────────────────────────────────────
        if self._info.kind == UpdateKind.PATCH:
            body_text = self.tr(
                "A new update is available for Solin.\n"
                "The download is quick and the app will restart automatically."
            )
        else:
            body_text = self.tr(
                "A new full version of Solin is available.\n"
                "Click 'Download' to open the download page."
            )

        body = QLabel(body_text)
        body.setObjectName("body")
        body.setWordWrap(True)
        lay.addWidget(body)

        # ── Progress bar (patch only) ──────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setVisible(False)
        lay.addWidget(self._progress_bar)

        # ── Status label ───────────────────────────────────────────────────────
        self._status_label = QLabel("")
        self._status_label.setObjectName("body")
        self._status_label.setVisible(False)
        lay.addWidget(self._status_label)

        # ── Buttons ────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._btn_cancel = QPushButton(self.tr("Not now"))
        self._btn_cancel.setObjectName("btn_secondary")
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._btn_cancel)

        if self._info.kind == UpdateKind.PATCH:
            self._btn_action = QPushButton(self.tr("Update now"))
        else:
            self._btn_action = QPushButton(self.tr("Download"))

        self._btn_action.setObjectName("btn_primary")
        self._btn_action.clicked.connect(self._on_action)
        btn_row.addWidget(self._btn_action)

        lay.addLayout(btn_row)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_cancel(self) -> None:
        if self._downloader:
            self._downloader.abort()
        self.close()

    def _on_action(self) -> None:
        from solin.core.remote.update_policy import UpdateKind
        if self._info.kind == UpdateKind.SETUP:
            QDesktopServices.openUrl(QUrl(self._info.url))
            self.close()
        else:
            self._start_download()

    def _start_download(self) -> None:
        self._btn_action.setEnabled(False)
        self._btn_action.setText(self.tr("Downloading…"))
        self._btn_cancel.setEnabled(False)

        self._progress_bar.setVisible(True)
        self._status_label.setVisible(True)
        self._status_label.setText(self.tr("Starting download…"))

        self._downloader = self._patch_downloader_factory(self._info.url, self)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.finished.connect(self._on_download_done)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.start()

    def _on_progress(self, pct: int) -> None:
        self._progress_bar.setValue(pct)
        self._status_label.setText(self.tr("Downloading… %1%").replace("%1", str(pct)))

    def _on_download_done(self, path: str) -> None:
        self._patch_path = path
        self._progress_bar.setValue(100)
        self._status_label.setText(self.tr("Completed. Applying update…"))
        self._btn_cancel.setEnabled(False)

        self._save_cleanup_path(path)

        from PySide6.QtCore import QTimer
        QTimer.singleShot(800, self._launch_patch_and_quit)

    def _on_download_failed(self, msg: str) -> None:
        log.warning("[Update] download failed: %s", msg)
        self._progress_bar.setVisible(False)
        self._status_label.setVisible(True)
        self._status_label.setStyleSheet(f"color:{_C['red']}; background:transparent;")
        self._status_label.setText(self.tr("Download failed. Check your connection."))
        self._btn_action.setEnabled(True)
        self._btn_action.setText(self.tr("Try again"))
        self._btn_cancel.setEnabled(True)
        self._downloader = None

    def _launch_patch_and_quit(self) -> None:
        """
        Launches the patch with /SILENT /CLOSEAPPLICATIONS and exits this process.
        The patch.iss [Run] section is configured to reopen Solin after install.
        """
        try:
            self._launch_patch(self._patch_path)
        except OSError as exc:
            log.error("[Update] failed to launch patch: %s", exc)
            self._status_label.setStyleSheet(f"color:{_C['red']}; background:transparent;")
            self._status_label.setText(self.tr("Failed to launch installer."))
            return

        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    # ── Drag to move ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = True
            # Store the offset between the cursor and the top-left of the window
            self._drag_start_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_active and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # ── Fade-in animation ──────────────────────────────────────────────────────

    def _fade_in(self) -> None:
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, QByteArray(b"opacity"), self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # ── Initial centering ──────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.parent():
            p = self.parent()
            geo = p.geometry()
            x = geo.x() + (geo.width()  - self.width())  // 2
            y = geo.y() + (geo.height() - self.height()) // 2
            self.move(x, y)
