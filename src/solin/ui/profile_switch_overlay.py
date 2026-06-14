"""
profile_switch_overlay.py — Solin
===================================
Overlay de troca de perfil que cobre o MainWindow inteiro.
Exibido como widget-filho (não uma nova janela) ao clicar no avatar de perfil.

Fluxo:
  • Usuário clica no avatar → overlay cobre o MainWindow inteiro
  • Clica em perfil diferente  → emite `profile_selected(profile_id)`
  • Clica no card pontilhado   → emite `create_profile_requested`
  • Clica no mesmo perfil / X / Esc → emite `cancelled`
"""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal, QEvent, QRectF, QSize
from PySide6.QtGui import (
    QPainter, QColor, QFont, QFontMetrics, QPen, QBrush,
    QPainterPath, QLinearGradient, QKeyEvent,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)

from ..core.ui.helpers import avatar_colors as _avatar_colors
from ..core.ui.helpers import fade_in as _fade_in
from ..core.ui.helpers import initials as _initials
from ..core.profiles.models import ProfileInfo
from ..styles.icons import make_icon, ICON_OVERLAY_CLOSE

_BG     = "#0d1117"
_SURF   = "#161b22"
_CARD   = "#1c2128"
_BORDER = "#30363d"
_MUTED  = "#8b949e"
_TEXT   = "#e6edf3"
_DIM    = "#484f58"
_ACCENT = "#388bfd"
_GREEN  = "#3fb950"

class _SwitchProfileCard(QWidget):
    clicked = Signal(str)

    _W, _H       = 148, 172
    _AVATAR_R    = 48
    _BORDER_R    = 14
    _HOVER_SCALE = 1.04

    def __init__(self, profile, is_active: bool = False, parent=None):
        super().__init__(parent)
        self.profile    = profile
        self._is_active = is_active
        self._hovered   = False
        self._pressed   = False
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._c1, self._c2 = _avatar_colors(profile.id)

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

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale  = 0.97 if self._pressed else (self._HOVER_SCALE if self._hovered else 1.0)
        cx, cy = self._W / 2, self._H / 2
        p.translate(cx, cy)
        p.scale(scale, scale)
        p.translate(-cx, -cy)

        if self._is_active:
            bg_color = QColor("#182518")
            border_c = QColor(_GREEN)
            border_w = 2.0
        else:
            bg_color = QColor("#21262d") if self._hovered else QColor(_CARD)
            border_c = QColor(_ACCENT if self._hovered else _BORDER)
            border_w = 1.5

        path = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(border_c, border_w))
        p.drawPath(path)

        av_x = (self._W - self._AVATAR_R * 2) / 2
        av_y = 28
        grad = QLinearGradient(av_x, av_y, av_x + self._AVATAR_R * 2, av_y + self._AVATAR_R * 2)
        grad.setColorAt(0, QColor(self._c1))
        grad.setColorAt(1, QColor(self._c2))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(av_x, av_y, self._AVATAR_R * 2, self._AVATAR_R * 2))

        font = QFont("Segoe UI", 20, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(
            QRectF(av_x, av_y, self._AVATAR_R * 2, self._AVATAR_R * 2),
            Qt.AlignmentFlag.AlignCenter,
            _initials(self.profile.name),
        )

        if self._is_active:
            p.setBrush(QColor(_GREEN))
            p.setPen(QPen(QColor(_BG), 2.5))
            dot_x = av_x + self._AVATAR_R * 2 - 15
            dot_y = av_y + self._AVATAR_R * 2 - 15
            p.drawEllipse(QRectF(dot_x, dot_y, 14, 14))

        name_y = av_y + self._AVATAR_R * 2 + 14
        name_h = self._H - name_y - 12
        font2  = QFont("Segoe UI", 11)
        fm     = QFontMetrics(font2)
        elided = fm.elidedText(self.profile.name, Qt.TextElideMode.ElideRight, self._W - 20)
        p.setFont(font2)
        p.setPen(QColor(_GREEN if self._is_active else _TEXT))
        p.drawText(
            QRectF(0, name_y, self._W, name_h),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            elided,
        )
        p.end()


class _SwitchAddProfileCard(QWidget):
    """Card pontilhado para criar novo perfil, igual ao seletor inicial."""

    clicked = Signal()

    _W, _H    = 148, 172
    _BORDER_R = 14

    def __init__(self, label: str = "New Profile", parent=None):
        super().__init__(parent)
        self._label = label
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
            self.clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale = 0.97 if self._pressed else (1.04 if self._hovered else 1.0)
        cx, cy = self._W / 2, self._H / 2
        p.translate(cx, cy)
        p.scale(scale, scale)
        p.translate(-cx, -cy)

        pen = QPen(QColor(_ACCENT if self._hovered else _BORDER), 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor("#1a2130" if self._hovered else "transparent"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, self._W - 8, self._H - 8), self._BORDER_R, self._BORDER_R)
        p.drawPath(path)

        c = QColor(_ACCENT if self._hovered else _DIM)
        p.setPen(QPen(c, 2.5))
        cx2, cy2 = self._W / 2, self._H / 2 - 12
        r = 16
        p.drawLine(int(cx2 - r), int(cy2), int(cx2 + r), int(cy2))
        p.drawLine(int(cx2), int(cy2 - r), int(cx2), int(cy2 + r))

        font = QFont("Segoe UI", 11)
        p.setFont(font)
        p.setPen(QColor(_ACCENT if self._hovered else _MUTED))
        p.drawText(
            QRectF(0, cy2 + r + 10, self._W, 30),
            Qt.AlignmentFlag.AlignHCenter,
            self._label,
        )
        p.end()


class _CloseButton(QPushButton):
    """Botão X com ícone SVG — muda de cor no hover."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.tr("Cancel profile switch"))
        self.setFlat(True)

        self._icon_normal = make_icon(ICON_OVERLAY_CLOSE, 24, _MUTED)
        self._icon_hover  = make_icon(ICON_OVERLAY_CLOSE, 24, _TEXT)
        self.setIcon(self._icon_normal)
        self.setIconSize(QSize(24, 24))

        self.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 20px;
                padding: 4px;
            }
            QPushButton:hover { background: #21262d; }
            QPushButton:pressed { background: #161b22; }
        """)

    def enterEvent(self, event):
        self.setIcon(self._icon_hover)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setIcon(self._icon_normal)
        super().leaveEvent(event)


class ProfileSwitchOverlay(QWidget):
    """
    Overlay de troca de perfil. Deve ser filho direto do MainWindow.

    Signals
    -------
    cancelled
        Usuário cancelou (X / Esc / mesmo perfil).
    profile_selected(str)
        Um perfil diferente foi selecionado — emite o profile_id.
    create_profile_requested
        Usuário pediu criação de novo perfil.
    """

    cancelled        = Signal()
    profile_selected = Signal(str)
    create_profile_requested = Signal()

    def __init__(
        self,
        parent_window: QWidget,
        current_profile_id: str,
        *,
        profiles: Sequence[ProfileInfo],
    ):
        super().__init__(parent_window)
        self._current_id = current_profile_id
        self._profiles = tuple(profiles)

        self.setGeometry(parent_window.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QWidget {{ background: {_BG}; }}")

        parent_window.installEventFilter(self)

        self._build_ui()
        _fade_in(self, duration=180)

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
        return False

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(72)
        header.setStyleSheet(
            f"QWidget {{ background: {_SURF}; border-bottom: 1px solid {_BORDER}; }}"
        )
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(32, 0, 24, 0)
        hlay.setSpacing(0)

        logo = QLabel("Solin")
        logo.setStyleSheet(
            "color: #e6edf3; font-size: 22px; font-weight: 700; background: transparent;"
        )
        hlay.addWidget(logo)
        hlay.addStretch()

        self._close_btn = _CloseButton()
        self._close_btn.clicked.connect(self._cancel)
        hlay.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(header)

        # ── Body ──────────────────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet(f"QWidget {{ background: {_BG}; }}")
        blay = QVBoxLayout(body)
        blay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        blay.setContentsMargins(40, 60, 40, 40)
        blay.setSpacing(0)

        self._title_lbl = QLabel(self.tr("Who is using Solin?"))
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._title_lbl.setStyleSheet(
            "color: #e6edf3; font-size: 28px; font-weight: 700; background: transparent;"
        )
        blay.addWidget(self._title_lbl)
        blay.addSpacing(8)

        # self._subtitle_lbl = QLabel(self.tr("Select a profile to continue."))
        # self._subtitle_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # self._subtitle_lbl.setStyleSheet(
        #     f"color: {_MUTED}; font-size: 13px; background: transparent;"
        # )
        # blay.addWidget(self._subtitle_lbl)
        blay.addSpacing(44)

        grid = QWidget()
        grid.setStyleSheet("background: transparent;")
        grid_lay = QHBoxLayout(grid)
        grid_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        grid_lay.setSpacing(20)
        grid_lay.setContentsMargins(0, 0, 0, 0)

        for prof in self._profiles:
            card = _SwitchProfileCard(prof, is_active=(prof.id == self._current_id))
            card.clicked.connect(self._on_card_clicked)
            grid_lay.addWidget(card)

        self._add_card = _SwitchAddProfileCard(self.tr("New Profile"))
        self._add_card.clicked.connect(self._on_create_profile_clicked)
        grid_lay.addWidget(self._add_card)

        blay.addWidget(grid, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(body, 1)

    def _on_card_clicked(self, profile_id: str):
        if profile_id == self._current_id:
            self._cancel()
        else:
            self.profile_selected.emit(profile_id)

    def _on_create_profile_clicked(self):
        self.create_profile_requested.emit()

    def _cancel(self):
        self.cancelled.emit()

    def retranslateUi(self) -> None:
        self._close_btn.setToolTip(self.tr("Cancel profile switch"))
        self._title_lbl.setText(self.tr("Who is using Solin?"))
        self._add_card.set_label(self.tr("New Profile"))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)
