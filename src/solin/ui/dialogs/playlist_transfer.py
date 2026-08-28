"""Application-modal progress dialog for playlist transfers."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from solin.styles.theme import PALETTE

_TR_CONTEXT = "PlaylistTransfer"
_TITLE_SOURCE = QT_TRANSLATE_NOOP("PlaylistTransfer", "Playlist transfer")
_CANCEL_SOURCE = QT_TRANSLATE_NOOP("PlaylistTransfer", "Cancel")
_CANCELLING_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistTransfer",
    "Cancelling safely…",
)
_REMOVING_TEMP_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistTransfer",
    "Temporary files are being removed.",
)
_FAILED_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistTransfer",
    "The operation could not be completed.",
)
_CLOSE_SOURCE = QT_TRANSLATE_NOOP("PlaylistTransfer", "Close")


def _tr(text: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, text)


class PlaylistTransferDialog(QDialog):
    """Non-blocking modal that keeps the Qt event loop responsive."""

    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._can_cancel = True
        self._terminal = False
        self.setObjectName("PlaylistTransferDialog")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowTitle(_tr(_TITLE_SOURCE))
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        self._title = QLabel(self)
        self._title.setObjectName("TransferTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._title)

        self._stage = QLabel(self)
        self._stage.setObjectName("TransferStage")
        self._stage.setTextFormat(Qt.TextFormat.PlainText)
        self._stage.setWordWrap(True)
        layout.addWidget(self._stage)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(True)
        self._progress.setMinimumHeight(12)
        layout.addWidget(self._progress)

        self._detail = QLabel(self)
        self._detail.setObjectName("TransferDetail")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel = QPushButton(_tr(_CANCEL_SOURCE), self)
        self._cancel.clicked.connect(self._request_cancel)
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)
        self._apply_theme()

    def begin(self, title: str, stage: str) -> None:
        self._terminal = False
        self._can_cancel = True
        self._title.setText(title)
        self._stage.setText(stage)
        self._detail.clear()
        self._cancel.setText(_tr(_CANCEL_SOURCE))
        self._cancel.setEnabled(True)
        self._progress.setRange(0, 0)
        self._progress.setFormat("")
        self.open()

    def set_progress(
        self,
        *,
        stage: str,
        detail: str = "",
        completed: int = 0,
        total: int = 0,
        can_cancel: bool = True,
    ) -> None:
        if self._terminal:
            return
        self._stage.setText(stage)
        self._detail.setText(detail)
        self._can_cancel = can_cancel
        self._cancel.setEnabled(can_cancel)
        if total > 0:
            bounded = max(0, min(completed, total))
            self._progress.setRange(0, 1_000)
            self._progress.setValue(round(1_000 * bounded / total))
            self._progress.setFormat("%p%")
        else:
            self._progress.setRange(0, 0)
            self._progress.setFormat("")

    def show_cancelling(self) -> None:
        self._can_cancel = False
        self._cancel.setEnabled(False)
        self._stage.setText(_tr(_CANCELLING_SOURCE))
        self._detail.setText(_tr(_REMOVING_TEMP_SOURCE))

    def show_error(self, message: str) -> None:
        self._terminal = True
        self._can_cancel = False
        self._stage.setText(_tr(_FAILED_SOURCE))
        self._detail.setText(message)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFormat("")
        self._cancel.setEnabled(True)
        self._cancel.setText(_tr(_CLOSE_SOURCE))

    def finish(self) -> None:
        self._terminal = True
        self._can_cancel = False
        self.accept()

    def _request_cancel(self) -> None:
        if self._terminal:
            self.reject()
            return
        if self._can_cancel:
            self.cancel_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._terminal:
            event.accept()
            return
        if self._can_cancel:
            self.cancel_requested.emit()
        event.ignore()

    def reject(self) -> None:
        if self._terminal:
            super().reject()
        elif self._can_cancel:
            self.cancel_requested.emit()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"QDialog#PlaylistTransferDialog{{background:{PALETTE.bg1};}}"
            "QDialog#PlaylistTransferDialog QLabel{background:transparent;}"
            f"QLabel#TransferTitle{{color:{PALETTE.text_primary};font-size:16px;"
            "font-weight:700;}"
            f"QLabel#TransferStage{{color:{PALETTE.text_secondary};font-size:12px;}}"
            f"QLabel#TransferDetail{{color:{PALETTE.text_muted};font-size:11px;}}"
            f"QProgressBar{{background:{PALETTE.bg0};border:1px solid {PALETTE.border};"
            "border-radius:6px;min-height:12px;text-align:center;color:transparent;}"
            f"QProgressBar::chunk{{background:{PALETTE.accent};border-radius:5px;}}"
            f"QPushButton{{background:{PALETTE.bg2};color:{PALETTE.text_secondary};"
            f"border:1px solid {PALETTE.border};border-radius:6px;padding:7px 18px;}}"
            f"QPushButton:hover{{background:{PALETTE.bg3};border-color:{PALETTE.text_dim};}}"
            f"QPushButton:disabled{{color:{PALETTE.text_dim};}}"
        )


__all__ = ["PlaylistTransferDialog"]
