"""
ZoomPanel — floating popup showing Zoom meeting controls.

Displayed when the user clicks the Zoom icon in the QuickAccessToolbar.
Design mirrors OBSScenePopup for visual consistency — premium glassmorphism.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGraphicsOpacityEffect, QToolTip,
)
from PySide6.QtCore import (
    Qt, QSize, QPropertyAnimation, QEasingCurve, QEvent, Signal, QPoint,
)
from PySide6.QtGui import QGuiApplication

from ..styles.icons import (
    make_icon, ICON_ZOOM, ICON_PEOPLE,
    ICON_INFO_CIRCLE, ICON_SPEAKER_PHONE,
)
from ..styles.theme import PALETTE, qss_rgba


_BG = PALETTE.bg0
_SURF = PALETTE.surface
_BORDER = PALETTE.border_muted
_BORDER2 = PALETTE.border
_TEXT = PALETTE.text_primary
_MUTED = PALETTE.text_muted
_DIM = PALETTE.text_faint
_ACCENT = PALETTE.accent
_GREEN = PALETTE.success
_RED = PALETTE.danger


class ZoomPanel(QWidget):
    """
    Floating popup with Zoom meeting info & quick-actions.

    Shows:
      • Connection status dot + label
      • Participant count (SVG icon, tooltip with names list)
      • Sharing indicator badge
      • "Open audio for all" action button
      • Info icon with tooltip for name separator tips
    """

    open_audio_requested = Signal()

    _POP_W = 280

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Fade animation
        self._opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_eff)
        self._fade = QPropertyAnimation(self._opacity_eff, b"opacity")
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._connected = False
        self._participant_count = 0
        self._sharing = False

        self._build_ui()

    # ── Build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("ZoomPanelCard")
        self._card.setStyleSheet(
            "QFrame#ZoomPanelCard {"
            f"  background: {qss_rgba(PALETTE.surface, 0.96)};"
            f"  border: 1px solid {qss_rgba(PALETTE.border, 0.85)};"
            "  border-radius: 16px;"
            "}"
        )
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setStyleSheet("background: transparent;")
        h_lay = QHBoxLayout(hdr)
        h_lay.setContentsMargins(16, 14, 16, 6)
        h_lay.setSpacing(8)

        zoom_px = QLabel()
        zoom_px.setPixmap(make_icon(ICON_ZOOM, 15, _ACCENT).pixmap(15, 15))
        zoom_px.setFixedSize(15, 15)
        zoom_px.setStyleSheet("background: transparent;")

        self._title_lbl = QLabel(self.tr("Zoom Meeting"))
        self._title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 13px; font-weight: 600;"
            " letter-spacing: 0.2px; background: transparent;"
        )

        h_lay.addWidget(zoom_px)
        h_lay.addWidget(self._title_lbl)
        h_lay.addStretch()
        card_lay.addWidget(hdr)

        # ── Status row ────────────────────────────────────────────────────
        status_w = QWidget()
        status_w.setStyleSheet("background: transparent;")
        s_lay = QHBoxLayout(status_w)
        s_lay.setContentsMargins(16, 2, 16, 8)
        s_lay.setSpacing(6)

        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(12)
        self._status_dot.setStyleSheet(
            f"color: {_DIM}; font-size: 8px; background: transparent;"
        )

        self._status_lbl = QLabel(self.tr("Disconnected"))
        self._status_lbl.setStyleSheet(
            f"font-size: 11px; color: {_MUTED}; background: transparent;"
        )

        s_lay.addWidget(self._status_dot)
        s_lay.addWidget(self._status_lbl, stretch=1)
        card_lay.addWidget(status_w)

        # ── Separator thin ────────────────────────────────────────────────
        sep1 = QFrame()
        sep1.setFixedHeight(1)
        sep1.setStyleSheet(f"background: {qss_rgba(PALETTE.border, 0.5)}; border: none;")
        card_lay.addWidget(sep1)

        # ── Content area (participants + sharing) ─────────────────────────
        self._content_w = QWidget()
        self._content_w.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(self._content_w)
        c_lay.setContentsMargins(16, 12, 16, 10)
        c_lay.setSpacing(10)

        # Participant row
        self._part_row = QWidget()
        self._part_row.setStyleSheet("background: transparent;")
        p_lay = QHBoxLayout(self._part_row)
        p_lay.setContentsMargins(0, 0, 0, 0)
        p_lay.setSpacing(10)

        self._part_icon = QLabel()
        self._part_icon.setPixmap(
            make_icon(ICON_PEOPLE, 16, _MUTED).pixmap(16, 16)
        )
        self._part_icon.setFixedSize(16, 16)
        self._part_icon.setStyleSheet("background: transparent;")

        self._part_lbl = QLabel(self.tr("%n attendee(s)", "", 0))
        self._part_lbl.setStyleSheet(
            f"font-size: 14px; color: {_TEXT}; font-weight: 600;"
            " background: transparent;"
        )

        # Info button (ℹ️  → shows tooltip on hover)
        self._info_btn = QPushButton()
        self._info_btn.setFixedSize(18, 18)
        self._info_btn.setIcon(make_icon(ICON_INFO_CIRCLE, 14, _DIM))
        self._info_btn.setIconSize(QSize(14, 14))
        self._info_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
        self._info_btn.setStyleSheet(
            "QPushButton {"
            "  border: none; background: transparent; padding: 0;"
            "}"
            "QPushButton:hover {"
            f"  background: {qss_rgba(PALETTE.white, 0.06)}; border-radius: 9px;"
            "}"
        )
        self._info_btn.setToolTip("")  # set dynamically
        self._info_btn.enterEvent = self._show_info_tooltip

        p_lay.addWidget(self._part_icon)
        p_lay.addWidget(self._part_lbl, stretch=1)
        p_lay.addWidget(self._info_btn)

        self._part_row.setVisible(False)
        c_lay.addWidget(self._part_row)

        # Sharing badge
        self._share_w = QWidget()
        self._share_w.setStyleSheet("background: transparent;")
        sh_lay = QHBoxLayout(self._share_w)
        sh_lay.setContentsMargins(0, 0, 0, 0)
        sh_lay.setSpacing(6)

        share_dot = QLabel("●")
        share_dot.setFixedWidth(12)
        share_dot.setStyleSheet(
            f"font-size: 8px; color: {_GREEN}; background: transparent;"
        )
        self._share_lbl = QLabel(self.tr("Screen sharing active"))
        self._share_lbl.setStyleSheet(
            f"font-size: 11px; color: {_GREEN}; font-weight: 500;"
            " background: transparent;"
        )

        sh_lay.addWidget(share_dot)
        sh_lay.addWidget(self._share_lbl, stretch=1)
        self._share_w.setVisible(False)
        c_lay.addWidget(self._share_w)

        card_lay.addWidget(self._content_w)

        # ── Separator ─────────────────────────────────────────────────────
        self._sep2 = QFrame()
        self._sep2.setFixedHeight(1)
        self._sep2.setStyleSheet(f"background: {qss_rgba(PALETTE.border, 0.5)}; border: none;")
        card_lay.addWidget(self._sep2)

        # ── Action button ─────────────────────────────────────────────────
        actions_w = QWidget()
        actions_w.setStyleSheet("background: transparent;")
        a_lay = QVBoxLayout(actions_w)
        a_lay.setContentsMargins(12, 10, 12, 12)
        a_lay.setSpacing(0)

        self._audio_btn = QPushButton()
        self._audio_btn.setObjectName("ZoomAudioBtn")
        self._audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._audio_btn.setFixedHeight(36)
        self._audio_btn.setIcon(make_icon(ICON_SPEAKER_PHONE, 14, PALETTE.text_primary))
        self._audio_btn.setIconSize(QSize(14, 14))
        self._audio_btn.setText(self.tr("Open audio for all"))
        self._audio_btn.setStyleSheet(
            "QPushButton#ZoomAudioBtn {"
            f"  background: {qss_rgba(PALETTE.accent, 0.10)};"
            f"  border: 1px solid {qss_rgba(PALETTE.accent, 0.25)};"
            "  border-radius: 10px;"
            f"  color: {_ACCENT};"
            "  font-size: 12px; font-weight: 600;"
            "  padding: 0 16px;"
            "  text-align: center;"
            "}"
            "QPushButton#ZoomAudioBtn:hover {"
            f"  background: {qss_rgba(PALETTE.accent, 0.18)};"
            f"  border-color: {qss_rgba(PALETTE.accent, 0.45)};"
            "}"
            "QPushButton#ZoomAudioBtn:pressed {"
            f"  background: {qss_rgba(PALETTE.accent, 0.28)};"
            "}"
            "QPushButton#ZoomAudioBtn:disabled {"
            f"  color: {_DIM}; border-color: {qss_rgba(PALETTE.border, 0.5)};"
            "  background: transparent;"
            "}"
        )
        self._audio_btn.clicked.connect(self._on_audio_clicked)
        a_lay.addWidget(self._audio_btn)

        card_lay.addWidget(actions_w)
        root.addWidget(self._card)

    # ── Public API ────────────────────────────────────────────────────────

    def set_connected(self, connected: bool):
        self._connected = connected
        if connected:
            self._status_dot.setStyleSheet(
                f"color: {_GREEN}; font-size: 8px; background: transparent;"
            )
            self._status_lbl.setText(self.tr("Connected to meeting"))
            self._status_lbl.setStyleSheet(
                f"font-size: 11px; color: {_TEXT}; background: transparent;"
            )
        else:
            self._status_dot.setStyleSheet(
                f"color: {_DIM}; font-size: 8px; background: transparent;"
            )
            self._status_lbl.setText(self.tr("Disconnected"))
            self._status_lbl.setStyleSheet(
                f"font-size: 11px; color: {_MUTED}; background: transparent;"
            )
            self._part_row.setVisible(False)
            self._share_w.setVisible(False)
        self._audio_btn.setEnabled(connected)

    def set_participants(self, count: int, names: list[str]):
        self._participant_count = count
        self._part_row.setVisible(True)
        
        # Parâmetros: tr(texto_base, desambiguação, n)
        text = self.tr("%n attendee(s)", "", count)
        
        self._part_lbl.setText(text)
        # Tooltip com nomes dos participantes removida
        # if names:
        #     self._part_lbl.setToolTip("\n".join(names))
        # else:
        #     self._part_lbl.setToolTip("")

    def set_sharing(self, sharing: bool):
        self._sharing = sharing
        self._share_w.setVisible(sharing)
        if sharing:
            self._share_lbl.setText(self.tr("Screen sharing active"))
        # Disable audio-for-all during sharing — it's disruptive
        self._audio_btn.setEnabled(self._connected and not sharing)
        self.adjustSize()

    # ── Positioning ───────────────────────────────────────────────────────

    def show_above(self, anchor: QWidget):
        self.setFixedWidth(self._POP_W)
        self.adjustSize()
        g = anchor.mapToGlobal(anchor.rect().topLeft())
        x = g.x() + (anchor.width() - self.width()) // 2
        y = g.y() - self.height() - 10

        screen_geo = (
            anchor.screen().availableGeometry()
            if hasattr(anchor, "screen")
            else QGuiApplication.primaryScreen().availableGeometry()
        )
        if x < screen_geo.left() + 4:
            x = screen_geo.left() + 4
        if x + self.width() > screen_geo.right() - 4:
            x = screen_geo.right() - self.width() - 4
        if y < screen_geo.top() + 4:
            y = g.y() + anchor.height() + 10

        self.move(x, y)
        self._opacity_eff.setOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    # ── Internal ──────────────────────────────────────────────────────────

    def _on_audio_clicked(self):
        self.open_audio_requested.emit()
        self.close()

    def _show_info_tooltip(self, event):
        """Show an elegant tooltip near the info button."""
        tip_text = self.tr(
            "Attendance can be counted in two ways:\n"
            "use &, |, or + between names, like 'Felipe & Julia' = 2;\n"
            "or put a number at the end, like 'Family Alves 7' = 7.\n"
            "A number at the end has priority."
        )
        pos = self._info_btn.mapToGlobal(
            QPoint(self._info_btn.width() // 2, -4)
        )
        QToolTip.showText(pos, tip_text, self._info_btn)

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._title_lbl.setText(self.tr("Zoom Meeting"))
            self._audio_btn.setText(self.tr("Open audio for all"))
            if self._sharing:
                self._share_lbl.setText(self.tr("Screen sharing active"))
            # Refresh status text
            self.set_connected(self._connected)
            # Refresh participant text
            if self._part_row.isVisible():
                self.set_participants(
                    self._participant_count,
                    [],  # names tooltip doesn't need refresh
                )
        super().changeEvent(event)
