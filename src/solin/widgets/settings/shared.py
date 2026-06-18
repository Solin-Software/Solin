"""Shared controls used by settings pages."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    Property,
    Qt,
    QPropertyAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

SETTINGS_BG = "#0d1117"
SETTINGS_SURFACE = "#161b22"
SETTINGS_CARD = "#161b22"
SETTINGS_BORDER = "#21262d"
SETTINGS_ACCENT = "#388bfd"
SETTINGS_BORDER_STRONG = "#30363d"
SETTINGS_MUTED = "#8b949e"
SETTINGS_TEXT = "#e6edf3"
SETTINGS_DIM = "#484f58"
SETTINGS_SUCCESS = "#3fb950"
SETTINGS_DANGER = "#f85149"

__all__ = (
    "SETTINGS_ACCENT",
    "SETTINGS_BG",
    "SETTINGS_BORDER",
    "SETTINGS_BORDER_STRONG",
    "SETTINGS_CARD",
    "SETTINGS_DANGER",
    "SETTINGS_DIM",
    "SETTINGS_MUTED",
    "SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET",
    "SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET",
    "SETTINGS_SURFACE",
    "SETTINGS_SUCCESS",
    "SETTINGS_TEXT",
    "SettingsToggleSwitch",
)


class SettingsToggleSwitch(QWidget):
    toggled = Signal(bool)
    _TRACK_ON = QColor(SETTINGS_ACCENT)
    _TRACK_OFF = QColor(SETTINGS_BORDER_STRONG)
    _THUMB = QColor("#ffffff")
    _W, _H = 40, 22

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._anim_value: float = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"_thumb_pos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_thumb(self) -> float:
        return self._anim_value

    def _set_thumb(self, value: float) -> None:
        self._anim_value = value
        self.update()

    _thumb_pos = Property(float, _get_thumb, _set_thumb)

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool, animate: bool = True) -> None:
        if self._checked == value:
            return
        self._checked = value
        target = 1.0 if value else 0.0
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._anim_value)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim_value = target
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            new_state = not self._checked
            self.set_checked(new_state)
            self.toggled.emit(new_state)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._anim_value
        track = QColor(
            int(self._TRACK_OFF.red() + (self._TRACK_ON.red() - self._TRACK_OFF.red()) * t),
            int(self._TRACK_OFF.green() + (self._TRACK_ON.green() - self._TRACK_OFF.green()) * t),
            int(self._TRACK_OFF.blue() + (self._TRACK_ON.blue() - self._TRACK_OFF.blue()) * t),
        )
        painter.setBrush(track)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, self._W, self._H, self._H // 2, self._H // 2)
        margin = 3
        diameter = self._H - 2 * margin
        travel = self._W - 2 * margin - diameter
        x = margin + int(travel * t)
        painter.setBrush(self._THUMB)
        painter.drawEllipse(x, margin, diameter, diameter)
        painter.end()


SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {SETTINGS_ACCENT}; color: white;"
    " border: none; border-radius: 8px; font-weight: 600; }"
    "QPushButton:hover { background: #58a6ff; }"
    "QPushButton:pressed { background: #2f7be0; }"
)

SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {SETTINGS_BORDER}; color: #c9d1d9;"
    f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px; }}"
    f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; color: {SETTINGS_TEXT}; }}"
)
