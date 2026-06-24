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

from solin.styles.theme import PALETTE


class _SettingsColorToken:
    def __init__(self, palette_attr: str) -> None:
        self._palette_attr = palette_attr

    def value(self) -> str:
        return getattr(PALETTE, self._palette_attr)

    def __str__(self) -> str:
        return self.value()

    def __format__(self, spec: str) -> str:
        return format(self.value(), spec)

    def __eq__(self, other) -> bool:
        return self.value() == other

    def __hash__(self) -> int:
        return hash(self.value())

    def __repr__(self) -> str:
        return repr(self.value())


SETTINGS_BG = _SettingsColorToken("bg0")
SETTINGS_SURFACE = _SettingsColorToken("surface")
SETTINGS_CARD = _SettingsColorToken("surface")
SETTINGS_BORDER = _SettingsColorToken("border_muted")
SETTINGS_ACCENT = _SettingsColorToken("accent")
SETTINGS_ACCENT_MUTED = _SettingsColorToken("accent_muted")
SETTINGS_ACCENT_HOVER = _SettingsColorToken("accent_hover")
SETTINGS_ACCENT_PRESSED = _SettingsColorToken("accent_pressed")
SETTINGS_BORDER_STRONG = _SettingsColorToken("border")
SETTINGS_MUTED = _SettingsColorToken("text_muted")
SETTINGS_TEXT = _SettingsColorToken("text_primary")
SETTINGS_TEXT_SECONDARY = _SettingsColorToken("text_secondary")
SETTINGS_TEXT_ON_ACCENT = _SettingsColorToken("text_on_accent")
SETTINGS_DIM = _SettingsColorToken("text_dim")
SETTINGS_SUCCESS = _SettingsColorToken("success")
SETTINGS_WARNING_TEXT = _SettingsColorToken("warning_text")
SETTINGS_DANGER = _SettingsColorToken("danger")

__all__ = (
    "SETTINGS_ACCENT",
    "SETTINGS_ACCENT_MUTED",
    "SETTINGS_ACCENT_HOVER",
    "SETTINGS_ACCENT_PRESSED",
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
    "SETTINGS_TEXT_SECONDARY",
    "SETTINGS_TEXT_ON_ACCENT",
    "SETTINGS_WARNING_TEXT",
    "SettingsToggleSwitch",
    "settings_picker_primary_button_stylesheet",
    "settings_picker_secondary_button_stylesheet",
    "settings_compact_secondary_button_stylesheet",
)


class SettingsToggleSwitch(QWidget):
    toggled = Signal(bool)
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
        track_on = QColor(str(SETTINGS_ACCENT))
        track_off = QColor(str(SETTINGS_BORDER_STRONG))
        track = QColor(
            int(track_off.red() + (track_on.red() - track_off.red()) * t),
            int(track_off.green() + (track_on.green() - track_off.green()) * t),
            int(track_off.blue() + (track_on.blue() - track_off.blue()) * t),
        )
        painter.setBrush(track)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, self._W, self._H, self._H // 2, self._H // 2)
        margin = 3
        diameter = self._H - 2 * margin
        travel = self._W - 2 * margin - diameter
        x = margin + int(travel * t)
        painter.setBrush(QColor(PALETTE.white))
        painter.drawEllipse(x, margin, diameter, diameter)
        painter.end()


def settings_picker_primary_button_stylesheet() -> str:
    return (
        f"QPushButton {{ background: {SETTINGS_ACCENT}; color: {PALETTE.white};"
        " border: none; border-radius: 8px; font-weight: 600; }"
        f"QPushButton:hover {{ background: {PALETTE.accent_hover}; }}"
        f"QPushButton:pressed {{ background: {PALETTE.accent_pressed}; }}"
    )


def settings_picker_secondary_button_stylesheet() -> str:
    return (
        f"QPushButton {{ background: {SETTINGS_BORDER}; color: {PALETTE.text_secondary};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px; }}"
        f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; color: {SETTINGS_TEXT}; }}"
    )


SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET = settings_picker_primary_button_stylesheet()
SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET = settings_picker_secondary_button_stylesheet()


def settings_compact_secondary_button_stylesheet(
    *,
    radius: int = 6,
    font_size: int = 11,
    text_color: str = SETTINGS_TEXT_SECONDARY,
    hover_text_color: str | None = None,
    hover_border_color: str | None = None,
    include_disabled: bool = False,
) -> str:
    """Return the compact secondary button style used inside Settings cards."""

    hover_text = f" color: {hover_text_color};" if hover_text_color else ""
    hover_border = f" border-color: {hover_border_color};" if hover_border_color else ""
    stylesheet = (
        f"QPushButton {{ padding: 0 10px; font-size: {font_size}px;"
        f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: {radius}px;"
        f" background: {SETTINGS_BORDER}; color: {text_color}; }}"
        f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG};"
        f"{hover_text}{hover_border} }}"
    )
    if include_disabled:
        stylesheet += (
            f"QPushButton:disabled {{ color: {SETTINGS_DIM};"
            f" border-color: {SETTINGS_BORDER}; background: transparent; }}"
        )
    return stylesheet
