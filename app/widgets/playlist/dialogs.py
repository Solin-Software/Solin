from __future__ import annotations

import colorsys

from PySide6.QtCore import QPoint, Qt, Signal
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

from ...core.meetings.colors import _section_colors


class _NameDialog(QDialog):
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
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
            "QLabel{color:#c9d1d9;font-size:12px;background:transparent;}"
            "QLineEdit{background:#0d1117;border:1px solid #30363d;border-radius:6px;"
            "color:#e6edf3;font-size:13px;padding:6px 10px;}"
            "QLineEdit:focus{border-color:#388bfd;}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(QLabel(label_str))

        self.name_edit = QLineEdit(initial)
        self.name_edit.setPlaceholderText(placeholder_str)
        self.name_edit.selectAll()
        lay.addWidget(self.name_edit)

        row = QHBoxLayout()
        ok = QPushButton(ok_str)
        cancel = QPushButton(cancel_str)
        ok.setFixedHeight(30)
        cancel.setFixedHeight(30)
        ok.setStyleSheet(
            "QPushButton{border:1px solid #388bfd;border-radius:6px;"
            "background:#1f3a5f;padding:0 16px;color:#79c0ff;font-size:11px;font-weight:600;}"
            "QPushButton:hover{background:#2a4f7f;}"
        )
        cancel.setStyleSheet(
            "QPushButton{border:1px solid #30363d;border-radius:6px;"
            "background:#21262d;padding:0 16px;color:#8b949e;font-size:11px;}"
            "QPushButton:hover{background:#2d333b;color:#c9d1d9;}"
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


class _HuePickerDialog(QDialog):
    """Minimal hue picker with a Photoshop-style gradient slider."""

    def __init__(self, initial_hue: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Section color"))
        self.setModal(True)
        self.setFixedWidth(300)
        self.setStyleSheet(
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:10px;}"
            "QLabel{color:#c9d1d9;font-size:11px;background:transparent;}"
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
            "QPushButton{border:1px solid #388bfd;border-radius:6px;"
            "background:#1f3a5f;padding:0 16px;color:#79c0ff;font-size:11px;font-weight:600;}"
            "QPushButton:hover{background:#2a4f7f;}"
        )
        cancel.setStyleSheet(
            "QPushButton{border:1px solid #30363d;border-radius:6px;"
            "background:#21262d;padding:0 16px;color:#8b949e;font-size:11px;}"
            "QPushButton:hover{background:#2d333b;color:#c9d1d9;}"
        )
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _swatch_css(self, hue: int) -> str:
        colors = _section_colors(hue)
        return (
            f"background:{colors['bg']};"
            f"border:2px solid {colors['accent']};"
            "border-radius:8px;"
        )

    def _on_hue(self, hue: int):
        self._preview.setStyleSheet(self._swatch_css(hue))

    def selected_hue(self) -> int:
        return self._slider.hue()
