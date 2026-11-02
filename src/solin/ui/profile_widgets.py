"""Shared profile tiles, dialogs, and style helpers."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QContextMenuEvent,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core.profiles.models import ProfileInfo
from .helpers import avatar_colors
from .helpers import initials
from ..styles.theme import PALETTE, ThemeColorToken, palette_token

# ── Cores ────────────────────────────────────────────────────────────────────
PROFILE_BG = palette_token("bg0")
PROFILE_SURFACE = palette_token("surface")
PROFILE_CARD = palette_token("surface_hover_strong")
PROFILE_BORDER = palette_token("border")
PROFILE_MUTED = palette_token("text_muted")
PROFILE_TEXT = palette_token("text_primary")
PROFILE_DIM = palette_token("text_dim")
PROFILE_ACCENT = palette_token("accent")
PROFILE_DANGER = palette_token("danger")


class _ProfileButton(QAbstractButton):
    """Shared button semantics and painting for profile tiles."""

    _W, _H = 148, 172
    _BORDER_R = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(self._W, self._H)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def _painter(self, *, dashed: bool = False, current: bool = False) -> QPainter:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.5)
        highlighted = self.underMouse() or self.hasFocus()
        background = (
            PALETTE.success_surface
            if current
            else PALETTE.accent_tint
            if dashed and highlighted
            else "transparent"
            if dashed
            else PALETTE.bg2
            if highlighted
            else PROFILE_CARD
        )
        border = (
            PROFILE_ACCENT
            if self.hasFocus()
            else PALETTE.success
            if current
            else PROFILE_ACCENT
            if highlighted
            else PROFILE_BORDER
        )
        pen = QPen(QColor(str(border)), 2 if current or self.hasFocus() or dashed else 1.5)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(str(background)))
        path = QPainterPath()
        inset = 6 if self.isDown() else 4
        path.addRoundedRect(
            QRectF(inset, inset, self._W - inset * 2, self._H - inset * 2),
            self._BORDER_R,
            self._BORDER_R,
        )
        painter.drawPath(path)
        if self.hasFocus():
            painter.setPen(QPen(QColor(str(PROFILE_ACCENT)), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(1, 1, self._W - 2, self._H - 2), 16, 16)
        return painter


class ProfileCard(_ProfileButton):
    """Profile tile with optional current-profile and management states."""

    context_requested = Signal(str, object)

    def __init__(
        self,
        profile: ProfileInfo,
        parent=None,
        *,
        is_current: bool = False,
        allow_management: bool = False,
    ):
        super().__init__(parent)
        self.profile = profile
        self._is_current = is_current
        self._allow_management = allow_management
        self.setText(profile.name.replace("&", "&&"))
        self.setAccessibleName(profile.name)
        self.setToolTip(profile.name)
        self._c1, self._c2 = avatar_colors(profile.id)

    def contextMenuEvent(self, event):
        if not self._allow_management:
            event.ignore()
            return
        position = (
            self.mapToGlobal(self.rect().center())
            if event.reason() == QContextMenuEvent.Reason.Keyboard
            else event.globalPos()
        )
        self.context_requested.emit(self.profile.id, position)
        event.accept()

    def paintEvent(self, event):
        painter = self._painter(current=self._is_current)
        avatar = QRectF((self._W - 96) / 2, 28, 96, 96)
        gradient = QLinearGradient(avatar.topLeft(), avatar.bottomRight())
        gradient.setColorAt(0, QColor(self._c1))
        gradient.setColorAt(1, QColor(self._c2))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(avatar)
        font = QFont(self.font())
        font.setPointSize(20)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(PALETTE.white))
        painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, initials(self.profile.name))
        if self._is_current:
            badge = QRectF(103, 103, 22, 22)
            painter.setBrush(QColor(PALETTE.success))
            painter.setPen(QPen(QColor(str(PROFILE_BG)), 2))
            painter.drawEllipse(badge)
            painter.setPen(
                QPen(
                    QColor(PALETTE.white),
                    2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawLine(109, 114, 112, 117)
            painter.drawLine(112, 117, 119, 110)
        font.setPointSize(11)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(str(PROFILE_TEXT)))
        name = QFontMetrics(font).elidedText(
            self.profile.name,
            Qt.TextElideMode.ElideRight,
            self._W - 20,
        )
        painter.drawText(
            QRectF(10, 138, self._W - 20, 26),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            name,
        )
        painter.end()


class AddProfileCard(_ProfileButton):
    """Dashed button for creating a profile."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.set_label(label)

    def set_label(self, label: str) -> None:
        self.setText(label)
        self.setToolTip(label)
        self.update()

    def paintEvent(self, event):
        painter = self._painter(dashed=True)
        highlighted = self.underMouse() or self.hasFocus()
        color = PROFILE_ACCENT if highlighted else PROFILE_MUTED
        painter.setPen(
            QPen(QColor(str(color)), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawLine(58, 72, 90, 72)
        painter.drawLine(74, 56, 74, 88)
        font = QFont(self.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QColor(str(color)))
        painter.drawText(
            QRectF(10, 106, self._W - 20, 52),
            Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
            self.text(),
        )
        painter.end()


# ── Helpers de estilo ─────────────────────────────────────────────────────────


def profile_button(text: str, primary: bool = True) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(42)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setStyleSheet(f"""
            QPushButton {{
                background: {PROFILE_ACCENT};
                color: {PALETTE.white};
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 24px;
            }}
            QPushButton:hover {{ background: {PROFILE_ACCENT}; }}
            QPushButton:pressed {{ background: {PALETTE.accent_pressed}; }}
            QPushButton:disabled {{ background: {PROFILE_DIM}; color: {PROFILE_MUTED}; }}
        """)
    else:
        b.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {PROFILE_MUTED};
                border: 1px solid {PROFILE_BORDER};
                border-radius: 8px;
                font-size: 13px;
                padding: 0 20px;
            }}
            QPushButton:hover {{
                background: {PROFILE_SURFACE};
                color: {PROFILE_TEXT};
                border-color: {PROFILE_MUTED};
            }}
            QPushButton:pressed {{ background: {PALETTE.bg2}; }}
        """)
    return b


def profile_field(placeholder: str = "") -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setFixedHeight(44)
    f.setStyleSheet(f"""
        QLineEdit {{
            background: {PALETTE.surface_card};
            color: {PROFILE_TEXT};
            border: 1.5px solid {PALETTE.border_muted};
            border-radius: 10px;
            font-size: 14px;
            padding: 0 14px;
        }}
        QLineEdit:focus {{
            border-color: {PROFILE_ACCENT};
            background: {PALETTE.surface_hover};
        }}
        QLineEdit::placeholder {{ color: {PROFILE_DIM}; }}
    """)
    return f


def profile_label(
    text: str,
    size: int = 13,
    color: str | ThemeColorToken = PROFILE_TEXT,
    weight: int = 400,
    align=Qt.AlignmentFlag.AlignLeft,
) -> QLabel:
    l = QLabel(text)
    l.setWordWrap(True)
    l.setAlignment(align)
    l.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight}; "
        "background: transparent; border: none;"
    )
    return l


class ProfileNameDialog(QDialog):
    def __init__(
        self,
        current: str = "",
        parent=None,
        *,
        title: str,
        label: str,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet(f"background: {PROFILE_SURFACE}; color: {PROFILE_TEXT};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {PROFILE_TEXT}; font-size: 16px; font-weight: 700; background: transparent;"
        )
        lay.addWidget(title_label)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {PROFILE_MUTED}; font-size: 12px; background: transparent;")
        lay.addWidget(lbl)

        self._field = profile_field()
        self._field.setText(current)
        self._field.selectAll()
        lay.addWidget(self._field)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = profile_button(self.tr("Cancel"), primary=False)
        save = profile_button(self.tr("Save"), primary=True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._accept_if_valid)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        lay.addLayout(btn_row)

        QTimer.singleShot(0, self._field.setFocus)

    def _accept_if_valid(self) -> None:
        if self.result_name():
            self.accept()

    def result_name(self) -> str:
        return self._field.text().strip()


__all__ = [
    "AddProfileCard",
    "ProfileCard",
    "PROFILE_ACCENT",
    "PROFILE_BG",
    "PROFILE_BORDER",
    "PROFILE_CARD",
    "PROFILE_DIM",
    "PROFILE_MUTED",
    "ProfileNameDialog",
    "PROFILE_DANGER",
    "PROFILE_SURFACE",
    "PROFILE_TEXT",
    "profile_button",
    "profile_field",
    "profile_label",
]
