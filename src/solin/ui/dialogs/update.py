"""Release notes and explicit download/install actions for verified packages."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    Qt,
    QUrl,
    QObject,
    QPropertyAnimation,
    QEasingCurve,
    QByteArray,
    QPoint,
)
from PySide6.QtGui import QDesktopServices, QMouseEvent, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QFrame,
    QGraphicsOpacityEffect,
    QTextBrowser,
)

if TYPE_CHECKING:
    from solin.core.remote.update_download import UpdateDownloadWorker
    from solin.core.remote.update_policy import UpdateInfo

from solin.core.remote.urls import is_safe_remote_url
from solin.styles.theme import PALETTE, qss_rgba

log = logging.getLogger(__name__)

_C = {
    "bg0": PALETTE.bg0,
    "bg1": PALETTE.bg1,
    "bg2": PALETTE.bg2,
    "border": PALETTE.border,
    "text": PALETTE.text_primary,
    "muted": PALETTE.text_muted,
    "accent": PALETTE.accent,
    "green": PALETTE.success,
    "yellow": PALETTE.warning,
    "red": PALETTE.danger,
}


class _ReleaseNotesBrowser(QTextBrowser):
    """Markdown viewer that never fetches embedded image resources."""

    def loadResource(self, resource_type: int, name: QUrl):  # noqa: N802 - Qt override
        if resource_type == QTextDocument.ResourceType.ImageResource:
            return None
        return super().loadResource(resource_type, name)


# The QDialog itself must be transparent (WA_TranslucentBackground is set).
# Only QFrame#card carries the actual background color — this prevents the
# background mismatch caused by the OS compositing the dialog's own bg
# on top of the card's bg.
_QSS = f"""
QDialog {{
    background: transparent;
}}
QFrame#card {{
    background: {_C["bg1"]};
    border: 1px solid {_C["border"]};
    border-radius: 10px;
}}
QLabel {{
    background: transparent;
}}
QLabel#title {{
    color: {_C["text"]};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#body {{
    color: {_C["muted"]};
    font-size: 12px;
}}
QLabel#version_badge {{
    color: {_C["green"]};
    font-size: 11px;
    font-weight: 600;
    background: {qss_rgba(PALETTE.success, 0.12)};
    border: 1px solid {qss_rgba(PALETTE.success, 0.3)};
    border-radius: 4px;
    padding: 2px 8px;
}}
QLabel#section_title {{
    color: {_C["text"]};
    font-size: 12px;
    font-weight: 700;
}}
QTextBrowser#changelog {{
    background: {_C["bg0"]};
    color: {_C["text"]};
    border: 1px solid {_C["border"]};
    border-radius: 7px;
    padding: 10px 12px;
    selection-background-color: {_C["accent"]};
}}
QPushButton#btn_primary {{
    background: {_C["accent"]};
    color: {PALETTE.white};
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-size: 12px;
    font-weight: 600;
    min-width: 100px;
}}
QPushButton#btn_primary:hover   {{ background: {PALETTE.accent_hover}; }}
QPushButton#btn_primary:pressed {{ background: {PALETTE.accent_pressed}; }}
QPushButton#btn_secondary {{
    background: transparent;
    color: {_C["muted"]};
    border: 1px solid {_C["border"]};
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 12px;
    min-width: 80px;
}}
QPushButton#btn_secondary:hover {{ color: {_C["text"]}; border-color: {_C["muted"]}; }}
QProgressBar {{
    background: {_C["bg2"]};
    border: 1px solid {_C["border"]};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {_C["accent"]};
    border-radius: 3px;
}}
"""

# ── Update dialog ─────────────────────────────────────────────────────────────


class UpdateDialog(QDialog):
    """Non-modal presentation with cancellable download and explicit installation."""

    def __init__(
        self,
        info: "UpdateInfo",
        parent=None,
        *,
        downloader_factory: Callable[[UpdateInfo, QObject], UpdateDownloadWorker],
        launch_installer: Callable[[str], None],
    ) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self._info = info
        self._downloader_factory = downloader_factory
        self._launch_installer = launch_installer
        self._downloader: UpdateDownloadWorker | None = None
        self._package_path = ""

        # Drag state
        self._drag_active = False
        self._drag_start_pos = QPoint()

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False)
        self.setMinimumWidth(520)

        self._build_ui()
        self.setStyleSheet(_QSS)
        self._fade_in()

    @property
    def downloaded_path(self) -> str:
        """Verified package path, empty until the transfer is complete."""
        return self._package_path

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
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

        badge = QLabel(f"v{self._info.version.display_version}")
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
        body_text = self.tr("A new version of Solin is available. Download it to continue.")

        body = QLabel(body_text)
        body.setObjectName("body")
        body.setWordWrap(True)
        lay.addWidget(body)

        self._changelog_browser: QTextBrowser | None = None
        if self._info.changelog:
            changelog_title = QLabel(self.tr("Changelog"))
            changelog_title.setObjectName("section_title")
            lay.addWidget(changelog_title)

            browser = _ReleaseNotesBrowser()
            browser.setObjectName("changelog")
            browser.setReadOnly(True)
            browser.setOpenExternalLinks(False)
            browser.setMinimumHeight(150)
            browser.setMaximumHeight(300)
            browser.document().setDefaultStyleSheet(
                f"h2 {{ color: {_C['text']}; font-size: 15px; margin: 4px 0 8px 0; }}"
                f"p, li {{ color: {_C['text']}; font-size: 12px; }}"
                f"a {{ color: {_C['accent']}; text-decoration: none; }}"
                f"code {{ color: {_C['yellow']}; background: {_C['bg2']}; }}"
                f"hr {{ color: {_C['border']}; }}"
            )
            browser.setMarkdown(self._changelog_markdown())
            browser.anchorClicked.connect(self._open_changelog_link)
            lay.addWidget(browser)
            self._changelog_browser = browser

        # ── Download progress ──────────────────────────────────────────
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

        self._btn_action = QPushButton(self.tr("Download"))

        self._btn_action.setObjectName("btn_primary")
        self._btn_action.clicked.connect(self._on_action)
        btn_row.addWidget(self._btn_action)

        lay.addLayout(btn_row)

    def _changelog_markdown(self) -> str:
        return "\n\n---\n\n".join(
            f"## v{note.version.display_version}\n\n{note.markdown}"
            for note in self._info.changelog
        )

    @staticmethod
    def _open_changelog_link(url: QUrl) -> None:
        if is_safe_remote_url(url.toString()):
            QDesktopServices.openUrl(url)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_cancel(self) -> None:
        if self._downloader:
            self._downloader.abort()
        self.close()

    def _on_action(self) -> None:
        if self._package_path:
            self._open_package()
        else:
            self._start_download()

    def _start_download(self) -> None:
        self._btn_action.setEnabled(False)
        self._btn_action.setText(self.tr("Downloading…"))
        self._btn_cancel.setEnabled(True)

        self._progress_bar.setVisible(True)
        self._status_label.setVisible(True)
        self._status_label.setText(self.tr("Starting download…"))

        self._downloader = self._downloader_factory(self._info, self)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.finished.connect(self._on_download_done)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.start()

    def _on_progress(self, pct: int) -> None:
        self._progress_bar.setValue(pct)
        self._status_label.setText(self.tr("Downloading… %1%").replace("%1", str(pct)))

    def _on_download_done(self, path: str) -> None:
        from solin.core.remote.update_policy import UpdateAction

        self._downloader = None
        self._package_path = path
        self._progress_bar.setValue(100)
        self._status_label.setText(self.tr("Download verified. Ready to install."))
        self._btn_cancel.setEnabled(True)
        self._btn_action.setEnabled(True)
        self._btn_action.setText(
            self.tr("Install and restart")
            if self._info.action == UpdateAction.INSTALL
            else self.tr("Open installer")
            if self._info.action == UpdateAction.OPEN
            else self.tr("Show in folder")
        )

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

    def _open_package(self) -> None:
        from pathlib import Path
        from solin.core.remote.update_policy import UpdateAction

        try:
            if self._info.action == UpdateAction.INSTALL:
                self._launch_installer(self._package_path)
                self._btn_action.setEnabled(False)
            else:
                path = Path(self._package_path)
                if self._info.action == UpdateAction.REVEAL:
                    path = path.parent
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    raise OSError("Unable to open downloaded package")
        except (OSError, RuntimeError) as exc:
            self.installation_failed(str(exc))

    def installation_failed(self, message: str) -> None:
        log.warning("[Update] installer failed: %s", message)
        self._status_label.setVisible(True)
        self._status_label.setText(self.tr("Failed to launch installer."))
        self._btn_cancel.setEnabled(True)
        self._btn_action.setEnabled(True)

    def reject(self) -> None:
        if self._downloader:
            self._downloader.abort()
        super().reject()

    def closeEvent(self, event) -> None:
        if self._downloader:
            self._downloader.abort()
        super().closeEvent(event)

    # ── Drag to move ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = True
            # Store the offset between the cursor and the top-left of the window
            self._drag_start_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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
        p = self.parent()
        if isinstance(p, QWidget):
            geo = p.geometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + (geo.height() - self.height()) // 2
            self.move(x, y)
