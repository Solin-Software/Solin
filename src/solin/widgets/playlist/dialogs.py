from __future__ import annotations

import colorsys
from collections.abc import Iterable

from PySide6.QtCore import QCoreApplication, QPoint, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.meetings.colors import section_colors
from ...core.foundation.folder_names import (
    FolderNameError,
    FolderNameIssue,
    ensure_available_folder_name,
)
from ...core.ingest.playlist_folder_names import (
    MeetingFolderNameError,
    normalize_playlist_folder_name,
)
from ...styles.theme import PALETTE, current_theme_scheme


class NameDialog(QDialog):
    def __init__(
        self,
        initial: str = "",
        lang=None,
        parent=None,
        label: str = "",
        placeholder: str = "",
    ):
        super().__init__(parent)
        title_str = self.tr("Playlist") if lang else "Playlist"
        label_str = label or (self.tr("Playlist name:") if lang else "Nome da playlist:")
        placeholder_str = placeholder or (
            self.tr("E.g.: Midweek Meeting") if lang else ""
        )
        ok_str = self.tr("OK") if lang else "OK"
        cancel_str = self.tr("Cancel") if lang else "Cancelar"
        self.setWindowTitle(title_str)
        self.setModal(True)
        self.setMinimumWidth(320)
        self.setStyleSheet(
            f"QDialog{{background:{PALETTE.surface};border:1px solid {PALETTE.border};border-radius:8px;}}"
            f"QLabel{{color:{PALETTE.text_secondary};font-size:12px;background:transparent;}}"
            f"QLineEdit{{background:{PALETTE.bg0};border:1px solid {PALETTE.border};border-radius:6px;"
            f"color:{PALETTE.text_primary};font-size:13px;padding:6px 10px;}}"
            f"QLineEdit:focus{{border-color:{PALETTE.accent};}}"
        )

        lay = QVBoxLayout(self)
        self._name_layout = lay
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel(label_str))

        self.name_edit = QLineEdit(initial)
        self.name_edit.setPlaceholderText(placeholder_str)
        self.name_edit.selectAll()
        lay.addWidget(self.name_edit)

        row = QHBoxLayout()
        ok = QPushButton(ok_str)
        self._ok_button = ok
        cancel = QPushButton(cancel_str)
        self._cancel_button = cancel
        ok.setFixedHeight(30)
        cancel.setFixedHeight(30)
        ok.setStyleSheet(
            f"QPushButton{{border:1px solid {PALETTE.accent};border-radius:6px;"
            f"background:{PALETTE.accent_muted};padding:0 16px;color:{PALETTE.accent_text};"
            "font-size:11px;font-weight:600;}"
            f"QPushButton:hover{{background:{PALETTE.accent_muted_hover};}}"
        )
        cancel.setStyleSheet(
            f"QPushButton{{border:1px solid {PALETTE.border};border-radius:6px;"
            f"background:{PALETTE.bg2};padding:0 16px;color:{PALETTE.text_muted};font-size:11px;}}"
            f"QPushButton:hover{{background:{PALETTE.bg3};color:{PALETTE.text_secondary};}}"
        )
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.name_edit.returnPressed.connect(self.accept)

    def get_name(self) -> str:
        return self.name_edit.text().strip()


class LinkedFolderNameDialog(NameDialog):
    """Validate folder components inline while preserving ordinary playlist names."""

    def __init__(
        self,
        initial: str = "",
        *,
        existing_names: Iterable[str] = (),
        excluding_name: str | None = None,
        lang=None,
        parent=None,
    ):
        self._existing_names = tuple(existing_names)
        self._excluding_name = excluding_name
        super().__init__(
            initial,
            lang=lang,
            parent=parent,
            label=QCoreApplication.translate("LinkedFolderNameDialog", "Folder name:"),
            placeholder=QCoreApplication.translate("LinkedFolderNameDialog", "E.g.: Special event"),
        )
        self.setMinimumWidth(380)
        self._ok_button.setText(QCoreApplication.translate("NameDialog", "OK"))
        self._cancel_button.setText(QCoreApplication.translate("NameDialog", "Cancel"))
        self._ok_button.setStyleSheet(
            self._ok_button.styleSheet()
            + f"QPushButton:disabled{{background:{PALETTE.bg2};color:{PALETTE.text_muted};"
            f"border-color:{PALETTE.border};}}"
        )
        self.setWindowTitle(self.tr("Create folder"))
        self.name_edit.setAccessibleName(self.tr("Folder name:"))
        # QLineEdit's default 32767-character limit accommodates the full
        # portable byte/unit limit; validation must never silently truncate it.
        self._error_label = QLabel(self)
        self._error_label.setWordWrap(True)
        self._error_label.setTextFormat(Qt.TextFormat.PlainText)
        self._error_label.setStyleSheet(
            f"color:{PALETTE.danger};font-size:12px;background:transparent;"
        )
        self._name_layout.insertWidget(2, self._error_label)
        self.name_edit.textChanged.connect(self._validate_name)
        self._validate_name()

    def get_name(self) -> str:
        name = normalize_playlist_folder_name(self.name_edit.text())
        return ensure_available_folder_name(
            name, self._existing_names, excluding_name=self._excluding_name
        )

    def _validate_name(self) -> bool:
        try:
            self.get_name()
        except FolderNameError as exc:
            messages = {
                FolderNameIssue.EMPTY: self.tr("Enter a folder name."),
                FolderNameIssue.WHITESPACE: self.tr(
                    "The name cannot start or end with whitespace."
                ),
                FolderNameIssue.CHARACTERS: self.tr(
                    'The name cannot contain / \\ < > : " | ? * or control characters.'
                ),
                FolderNameIssue.RESERVED: self.tr("This name is reserved by the operating system."),
                FolderNameIssue.HIDDEN: self.tr("The name cannot start with a dot."),
                FolderNameIssue.TRAILING_DOT: self.tr("The name cannot end with a dot."),
                FolderNameIssue.TOO_LONG: self.tr("This folder name is too long."),
                FolderNameIssue.CONFLICT: self.tr("A file or folder with this name already exists."),
            }
            message = messages[exc.issue] if self.name_edit.text() else ""
            if isinstance(exc, MeetingFolderNameError):
                message = self.tr(
                    "This name is reserved for meetings. Choose another folder name."
                )
            self._error_label.setText(message)
            self.name_edit.setAccessibleDescription(message)
            self._ok_button.setEnabled(False)
            return False
        self._error_label.clear()
        self.name_edit.setAccessibleDescription("")
        self._ok_button.setEnabled(True)
        return True

    def accept(self) -> None:
        if self._validate_name():
            super().accept()


class _HueSliderBar(QWidget):
    """Horizontal hue gradient bar with draggable handle."""

    hue_changed = Signal(int)

    def __init__(self, initial_hue: int = 0, parent=None):
        super().__init__(parent)
        self._hue = max(0, min(359, initial_hue))
        self.setFixedHeight(28)
        self.setMinimumWidth(240)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _hue_to_x(self, hue: int) -> int:
        margin = 8
        usable = self.width() - 2 * margin
        return margin + int(hue / 359 * usable)

    def _x_to_hue(self, x: int) -> int:
        margin = 8
        usable = self.width() - 2 * margin
        return max(0, min(359, int((x - margin) / max(1, usable) * 359)))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 8
        bar_h = 12
        bar_y = (self.height() - bar_h) // 2
        bar_w = self.width() - 2 * margin

        grad = QLinearGradient(margin, 0, margin + bar_w, 0)
        for i in range(7):
            hue = i * 60
            r, g, b = colorsys.hls_to_rgb(hue / 360, 0.5, 0.9)
            grad.setColorAt(i / 6, QColor(int(r * 255), int(g * 255), int(b * 255)))
        path = QPainterPath()
        path.addRoundedRect(margin, bar_y, bar_w, bar_h, 6, 6)
        painter.fillPath(path, QBrush(grad))

        hx = self._hue_to_x(self._hue)
        r, g, b = colorsys.hls_to_rgb(self._hue / 360, 0.5, 0.9)
        handle_color = QColor(int(r * 255), int(g * 255), int(b * 255))
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(QBrush(handle_color))
        painter.drawEllipse(QPoint(hx, self.height() // 2), 7, 7)
        painter.end()

    def mousePressEvent(self, event):
        self._update_hue(event.pos().x())

    def mouseMoveEvent(self, event):
        self._update_hue(event.pos().x())

    def _update_hue(self, x: int):
        new_hue = self._x_to_hue(x)
        if new_hue != self._hue:
            self._hue = new_hue
            self.update()
            self.hue_changed.emit(self._hue)

    def hue(self) -> int:
        return self._hue


class HuePickerDialog(QDialog):
    """Minimal hue picker with a Photoshop-style gradient slider."""

    def __init__(self, initial_hue: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Section color"))
        self.setModal(True)
        self.setFixedWidth(300)
        self.setStyleSheet(
            f"QDialog{{background:{PALETTE.surface};border:1px solid {PALETTE.border};border-radius:10px;}}"
            f"QLabel{{color:{PALETTE.text_secondary};font-size:11px;background:transparent;}}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(14)

        self._preview = QFrame(self)
        self._preview.setFixedHeight(36)
        self._preview.setStyleSheet(self._swatch_css(initial_hue))
        lay.addWidget(self._preview)

        self._slider = _HueSliderBar(initial_hue, self)
        self._slider.hue_changed.connect(self._on_hue)
        lay.addWidget(self._slider)

        row = QHBoxLayout()
        ok = QPushButton(self.tr("OK"))
        cancel = QPushButton(self.tr("Cancel"))
        ok.setFixedHeight(30)
        cancel.setFixedHeight(30)
        ok.setStyleSheet(
            f"QPushButton{{border:1px solid {PALETTE.accent};border-radius:6px;"
            f"background:{PALETTE.accent_muted};padding:0 16px;color:{PALETTE.accent_text};"
            "font-size:11px;font-weight:600;}"
            f"QPushButton:hover{{background:{PALETTE.accent_muted_hover};}}"
        )
        cancel.setStyleSheet(
            f"QPushButton{{border:1px solid {PALETTE.border};border-radius:6px;"
            f"background:{PALETTE.bg2};padding:0 16px;color:{PALETTE.text_muted};font-size:11px;}}"
            f"QPushButton:hover{{background:{PALETTE.bg3};color:{PALETTE.text_secondary};}}"
        )
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _swatch_css(self, hue: int) -> str:
        colors = section_colors(hue, current_theme_scheme())
        return (
            f"background:{colors['bg']};"
            f"border:2px solid {colors['accent']};"
            "border-radius:8px;"
        )

    def _on_hue(self, hue: int):
        self._preview.setStyleSheet(self._swatch_css(hue))

    def selected_hue(self) -> int:
        return self._slider.hue()
