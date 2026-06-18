"""Visual widgets and style helpers for ProfileScreen."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
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
    QFrame,
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
from ..styles.icons import make_icon
from ..styles.theme import SCROLLBAR_STYLESHEET

# ── Cores ────────────────────────────────────────────────────────────────────
PROFILE_BG      = "#0d1117"
PROFILE_SURFACE    = "#161b22"
PROFILE_CARD    = "#1c2128"
PROFILE_BORDER  = "#30363d"
PROFILE_MUTED   = "#8b949e"
PROFILE_TEXT    = "#e6edf3"
PROFILE_DIM     = "#484f58"
PROFILE_ACCENT  = "#388bfd"
PROFILE_SUCCESS   = "#3fb950"
PROFILE_DANGER     = "#f85149"
PROFILE_WARNING   = "#d29922"

PROFILE_SCROLLBAR_STYLESHEET = SCROLLBAR_STYLESHEET


def obs_field_style() -> str:
    return (
        f"QLineEdit {{ background: #13161c; color: {PROFILE_TEXT};"
        f" border: 1.5px solid #21262d; border-radius: 10px;"
        f" font-size: 13px; padding: 0 14px; }}"
        f"QLineEdit:focus {{ border-color: {PROFILE_ACCENT}; background: #181d27; }}"
    )


def obs_combo_style() -> str:
    return (
        f"QComboBox {{ background: #13161c; color: {PROFILE_TEXT};"
        f" border: 1.5px solid #21262d; border-radius: 10px;"
        f" font-size: 13px; padding: 0 14px; min-height: 44px; }}"
        f"QComboBox:focus {{ border-color: {PROFILE_ACCENT}; }}"
        f"QComboBox::drop-down {{ border: none; width: 28px; }}"
        f"QComboBox::down-arrow {{"
        f" border-left: 4px solid transparent;"
        f" border-right: 4px solid transparent;"
        f" border-top: 5px solid {PROFILE_MUTED}; margin-right: 12px; }}"
        f"QComboBox QAbstractItemView {{"
        f" background: {PROFILE_SURFACE}; color: {PROFILE_TEXT};"
        f" border: 1px solid #21262d;"
        f" selection-background-color: #1f3a6e; }}"
    )


# ── ProfileCard ───────────────────────────────────────────────────────────────

class ProfileCard(QWidget):
    """Card de perfil clicável com avatar e nome."""

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

        # Fundo do card
        bg_color = QColor("#21262d") if self._hovered else QColor(PROFILE_CARD)
        border_c = QColor(PROFILE_ACCENT if self._hovered else PROFILE_BORDER)
        path     = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(border_c, 1.5))
        p.drawPath(path)

        # Avatar (círculo gradiente)
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
        p.setPen(QColor("white"))
        p.drawText(
            QRectF(av_x, av_y, self._AVATAR_R * 2, self._AVATAR_R * 2),
            Qt.AlignmentFlag.AlignCenter,
            initials(self.profile.name),
        )

        # Nome
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
    """Card pontilhado para criar novo perfil."""

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
        p.setBrush(QColor("#1a2130" if self._hovered else "transparent"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.drawPath(path)

        # Ícone +
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


# ── StepProgress ──────────────────────────────────────────────────────────────

class StepProgress(QWidget):
    """Modern connected-dot progress indicator with gradient track."""

    def __init__(self, total: int, parent=None):
        super().__init__(parent)
        self._total = total
        self._current = 0
        self.setFixedHeight(24)
        self.setFixedWidth(200)

    def set_step(self, step: int) -> None:
        self._current = step
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        cy = self.height() / 2
        r_done = 5
        r_active = 6
        r_future = 4
        margin = 16
        usable = w - 2 * margin
        step_w = usable / (self._total - 1) if self._total > 1 else 0

        # Background track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(PROFILE_BORDER))
        p.drawRoundedRect(QRectF(margin, cy - 1, usable, 2), 1, 1)

        # Filled track (gradient)
        if self._current > 0:
            fill_w = step_w * self._current
            grad = QLinearGradient(margin, cy, margin + fill_w, cy)
            grad.setColorAt(0, QColor("#1f6feb"))
            grad.setColorAt(1, QColor(PROFILE_ACCENT))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(margin, cy - 1.5, fill_w, 3), 1.5, 1.5)

        for i in range(self._total):
            cx_dot = margin + step_w * i
            done = i < self._current
            active = i == self._current

            if done:
                p.setBrush(QColor(PROFILE_ACCENT))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(cx_dot, cy), r_done, r_done)
                # Check mark
                pen = QPen(QColor("white"), 1.5)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(
                    QPointF(cx_dot - 2.5, cy + 0.5),
                    QPointF(cx_dot - 0.5, cy + 2.5),
                )
                p.drawLine(
                    QPointF(cx_dot - 0.5, cy + 2.5),
                    QPointF(cx_dot + 3, cy - 1.5),
                )
            elif active:
                # Glow ring
                glow = QColor(PROFILE_ACCENT)
                glow.setAlpha(50)
                p.setBrush(glow)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(cx_dot, cy), r_active + 4, r_active + 4)
                # Inner dot
                p.setBrush(QColor(PROFILE_ACCENT))
                p.drawEllipse(QPointF(cx_dot, cy), r_active, r_active)
            else:
                p.setBrush(QColor(PROFILE_SURFACE))
                p.setPen(QPen(QColor(PROFILE_BORDER), 1.5))
                p.drawEllipse(QPointF(cx_dot, cy), r_future, r_future)

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
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 24px;
            }}
            QPushButton:hover {{ background: #4f9eff; }}
            QPushButton:pressed {{ background: #2d72d9; }}
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
            QPushButton:pressed {{ background: #21262d; }}
        """)
    return b


def profile_field(placeholder: str = "") -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setFixedHeight(44)
    f.setStyleSheet(f"""
        QLineEdit {{
            background: #13161c;
            color: {PROFILE_TEXT};
            border: 1.5px solid #21262d;
            border-radius: 10px;
            font-size: 14px;
            padding: 0 14px;
        }}
        QLineEdit:focus {{
            border-color: {PROFILE_ACCENT};
            background: #181d27;
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


def profile_section_card(title: str, icon: str, desc: str,
                  extra_widget: QWidget = None) -> QFrame:
    """Premium styled tutorial card with icon badge."""
    card = QFrame()
    card.setObjectName("SectionCard")
    card.setStyleSheet(f"""
        QFrame#SectionCard {{
            background: {PROFILE_SURFACE};
            border: 1px solid #21262d;
            border-radius: 14px;
        }}
    """)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(24, 20, 24, 20)
    lay.setSpacing(12)

    # Header: icon badge + title
    top = QHBoxLayout()
    top.setSpacing(12)
    icon_bg = QLabel()
    icon_bg.setFixedSize(38, 38)
    icon_bg.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon_bg.setStyleSheet(
        "background: #13161c; "
        "border: 1px solid #21262d; border-radius: 11px;"
    )
    # Render as SVG pixmap (clean, professional)
    pix = make_icon(icon, size=18, color=PROFILE_MUTED).pixmap(18, 18)
    icon_bg.setPixmap(pix)
    top.addWidget(icon_bg)

    title_label = profile_label(title, 15, PROFILE_TEXT, 600)
    top.addWidget(title_label, 1)
    lay.addLayout(top)

    description_label = QLabel(desc)
    description_label.setWordWrap(True)
    description_label.setStyleSheet(
        f"color: {PROFILE_MUTED}; font-size: 12.5px; "
        f"line-height: 18px; "
        "background: transparent; border: none;"
    )
    if desc:
        lay.addWidget(description_label)
    card.title_label = title_label
    card.description_label = description_label

    if extra_widget:
        lay.addSpacing(4)
        lay.addWidget(extra_widget)

    return card


class ProfileNameDialog(QDialog):
    def __init__(self, current: str = "", parent=None,
                 title: str = "Profile", label: str = "Profile name"):
        super().__init__(parent)
        self.setWindowTitle(self.tr(title))
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet(f"background: {PROFILE_SURFACE}; color: {PROFILE_TEXT};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(14)

        title_label = QLabel(self.tr(title))
        title_label.setStyleSheet(
            f"color: {PROFILE_TEXT}; font-size: 16px; font-weight: 700; background: transparent;"
        )
        lay.addWidget(title_label)

        lbl = QLabel(self.tr(label))
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

# ── Toggle simples para OBS no onboarding ─────────────────────────────────────

class OBSToggle(QWidget):
    toggled = Signal(bool)
    _W, _H = 40, 22

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self.update()
        self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(PROFILE_ACCENT if self._checked else PROFILE_BORDER)
        p.setBrush(track)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, self._W, self._H, self._H // 2, self._H // 2)
        margin = 3
        dia    = self._H - 2 * margin
        x      = (self._W - margin - dia) if self._checked else margin
        p.setBrush(QColor("white"))
        p.drawEllipse(x, margin, dia, dia)
        p.end()


# ── FlowLayout ─────────────────────────────────────────────────────────────────

class ProfileFlowLayout(QHBoxLayout):
    """
    Layout de fluxo simples (linha única) para os cards de perfil.
    Usa QHBoxLayout wrappado num QWidget; para grades maiores considere
    um layout de fluxo customizado.
    """

    def __init__(self, parent=None, h_spacing=16, v_spacing=16):
        # Usamos um grid simples centrado em vez de flow real
        super().__init__(parent)
        self.setSpacing(h_spacing)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

__all__ = [
    "AddProfileCard",
    "ProfileCard",
    "StepProgress",
    "PROFILE_ACCENT",
    "PROFILE_WARNING",
    "PROFILE_BG",
    "PROFILE_BORDER",
    "PROFILE_CARD",
    "PROFILE_DIM",
    "ProfileFlowLayout",
    "PROFILE_SUCCESS",
    "PROFILE_MUTED",
    "OBSToggle",
    "ProfileNameDialog",
    "PROFILE_DANGER",
    "PROFILE_SCROLLBAR_STYLESHEET",
    "PROFILE_SURFACE",
    "PROFILE_TEXT",
    "profile_button",
    "profile_field",
    "profile_label",
    "obs_combo_style",
    "obs_field_style",
    "profile_section_card",
]
