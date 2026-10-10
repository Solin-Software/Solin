"""Visual widgets and style helpers for ProfileScreen."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.profiles.models import ProfileInfo
from .helpers import avatar_colors
from .helpers import initials
from ..styles.theme import PALETTE

# ── Cores ────────────────────────────────────────────────────────────────────
PROFILE_BG = PALETTE.bg0
PROFILE_SURFACE = PALETTE.surface
PROFILE_CARD = PALETTE.surface_hover_strong
PROFILE_BORDER = PALETTE.border
PROFILE_MUTED = PALETTE.text_muted
PROFILE_TEXT = PALETTE.text_primary
PROFILE_DIM = PALETTE.text_dim
PROFILE_ACCENT = PALETTE.accent
PROFILE_DANGER = PALETTE.danger

# ── ProfileCard ───────────────────────────────────────────────────────────────

class ProfileCard(QWidget):
    """Clickable profile card with avatar and name."""

    clicked = Signal(str)   # profile_id
    context_requested = Signal(str, object)   # profile_id, global QPoint

    _W, _H        = 148, 172
    _AVATAR_R     = 48
    _BORDER_R     = 14
    _HOVER_SCALE  = 1.04

    def __init__(self, profile: ProfileInfo, parent=None):
        super().__init__(parent)
        self.profile   = profile
        self._hovered  = False
        self._pressed  = False
        self._scale    = 1.0
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMouseTracking(True)
        self._c1, self._c2 = avatar_colors(profile.id)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            self.clicked.emit(self.profile.id)

    def contextMenuEvent(self, event):
        self.context_requested.emit(self.profile.id, event.globalPos())
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale   = 0.97 if self._pressed else (self._HOVER_SCALE if self._hovered else 1.0)
        cx, cy  = self._W / 2, self._H / 2
        p.translate(cx, cy)
        p.scale(scale, scale)
        p.translate(-cx, -cy)

        # Card background
        bg_color = QColor(PALETTE.bg2) if self._hovered else QColor(PROFILE_CARD)
        border_c = QColor(PROFILE_ACCENT if self._hovered else PROFILE_BORDER)
        path     = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(border_c, 1.5))
        p.drawPath(path)

        # Avatar (gradient circle)
        av_x  = (self._W - self._AVATAR_R * 2) / 2
        av_y  = 28
        grad  = QLinearGradient(av_x, av_y, av_x + self._AVATAR_R * 2, av_y + self._AVATAR_R * 2)
        grad.setColorAt(0, QColor(self._c1))
        grad.setColorAt(1, QColor(self._c2))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(av_x, av_y, self._AVATAR_R * 2, self._AVATAR_R * 2))

        # Iniciais
        font = QFont("Segoe UI", 20, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor(PALETTE.white))
        p.drawText(
            QRectF(av_x, av_y, self._AVATAR_R * 2, self._AVATAR_R * 2),
            Qt.AlignmentFlag.AlignCenter,
            initials(self.profile.name),
        )

        # Name
        name_y  = av_y + self._AVATAR_R * 2 + 14
        name_h  = self._H - name_y - 12
        font2   = QFont("Segoe UI", 11, QFont.Weight.Normal)
        fm      = QFontMetrics(font2)
        elided  = fm.elidedText(self.profile.name, Qt.TextElideMode.ElideRight, self._W - 20)
        p.setFont(font2)
        p.setPen(QColor(PROFILE_TEXT))
        p.drawText(
            QRectF(0, name_y, self._W, name_h),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            elided,
        )
        p.end()


# ── AddProfileCard ────────────────────────────────────────────────────────────

class AddProfileCard(QWidget):
    """Dashed card for creating a profile."""

    clicked = Signal()

    _W, _H      = 148, 172
    _BORDER_R   = 14

    def __init__(self, label: str = "New Profile", parent=None):
        super().__init__(parent)
        self._label   = label
        self._hovered = False
        self._pressed = False
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_label(self, label: str) -> None:
        self._label = label
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            self.clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale  = 0.97 if self._pressed else (1.04 if self._hovered else 1.0)
        cx, cy = self._W / 2, self._H / 2
        p.translate(cx, cy)
        p.scale(scale, scale)
        p.translate(-cx, -cy)

        # Borda pontilhada
        pen = QPen(QColor(PROFILE_ACCENT if self._hovered else PROFILE_BORDER), 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(PALETTE.accent_tint if self._hovered else "transparent"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.drawPath(path)

        # Plus icon
        c = QColor(PROFILE_ACCENT if self._hovered else PROFILE_DIM)
        p.setPen(QPen(c, 2.5))
        cx2, cy2 = self._W / 2, self._H / 2 - 12
        r        = 16
        p.drawLine(int(cx2 - r), int(cy2), int(cx2 + r), int(cy2))
        p.drawLine(int(cx2), int(cy2 - r), int(cx2), int(cy2 + r))

        # Label
        font = QFont("Segoe UI", 11)
        p.setFont(font)
        p.setPen(QColor(PROFILE_ACCENT if self._hovered else PROFILE_MUTED))
        p.drawText(
            QRectF(0, cy2 + r + 10, self._W, 30),
            Qt.AlignmentFlag.AlignHCenter,
            self._label,
        )
        p.end()


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


def profile_label(text: str, size: int = 13, color: str = PROFILE_TEXT,
         weight: int = 400, align=Qt.AlignmentFlag.AlignLeft) -> QLabel:
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

# ── FlowLayout ─────────────────────────────────────────────────────────────────

class ProfileFlowLayout(QHBoxLayout):
    """
    Simple single-row flow layout for profile cards.
    Use QHBoxLayout wrapped in QWidget; consider a custom flow layout
    for larger grids.
    """

    def __init__(self, parent=None, h_spacing=16, v_spacing=16):
        # Use a simple centered grid rather than a full flow layout.
        super().__init__(parent)
        self.setSpacing(h_spacing)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

__all__ = [
    "AddProfileCard",
    "ProfileCard",
    "PROFILE_ACCENT",
    "PROFILE_BG",
    "PROFILE_BORDER",
    "PROFILE_CARD",
    "PROFILE_DIM",
    "ProfileFlowLayout",
    "PROFILE_MUTED",
    "ProfileNameDialog",
    "PROFILE_DANGER",
    "PROFILE_SURFACE",
    "PROFILE_TEXT",
    "profile_button",
    "profile_field",
    "profile_label",
]
