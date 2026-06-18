"""Profile avatar button used by the main sidebar."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

from solin.ui.helpers import avatar_colors, initials


class ProfileAvatarButton(QWidget):
    """Small circular profile switch button with deterministic initials."""

    clicked = Signal()

    _SIZE = 32

    def __init__(self, profile_name: str = "", parent=None):
        super().__init__(parent)
        self._name = profile_name
        self._hovered = False
        self.setFixedSize(self._SIZE + 4, self._SIZE + 4)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_name(self, name: str) -> None:
        self._name = name
        self.update()

    def _colors(self) -> tuple[str, str]:
        if not self._name:
            return "#30363d", "#484f58"
        return avatar_colors(self._name)

    def _initials(self) -> str:
        return initials(self._name or "?")

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        c1, c2 = self._colors()
        radius = self._SIZE // 2
        cx = self.width() / 2
        cy = self.height() / 2

        if self._hovered:
            painter.setPen(QPen(QColor("#388bfd"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(
                QRectF(
                    cx - radius - 2,
                    cy - radius - 2,
                    (radius + 2) * 2,
                    (radius + 2) * 2,
                )
            )

        gradient = QLinearGradient(cx - radius, cy - radius, cx + radius, cy + radius)
        gradient.setColorAt(0, QColor(c1))
        gradient.setColorAt(1, QColor(c2))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        painter.setPen(QColor("white"))
        painter.drawText(
            QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            self._initials(),
        )
        painter.end()
