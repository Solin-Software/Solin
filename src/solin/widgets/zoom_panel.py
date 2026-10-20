"""Floating audio/video workflows for Windows Zoom meetings."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from solin.styles.icons import ICON_PEOPLE, ICON_VIDEO, ICON_ZOOM, make_icon
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip


class ZoomPanel(QWidget):
    before_meeting_requested = Signal()
    meeting_start_requested = Signal()
    after_meeting_requested = Signal()
    background_song_link_toggled = Signal(bool)
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._connected = False
        self._busy = False
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)
        self._card = QFrame(self)
        self._card.setObjectName("ZoomPanelCard")
        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(16, 14, 16, 10)
        header.setSpacing(8)
        self._header_icon = QLabel(self._card)
        self._header_icon.setFixedSize(15, 15)
        header.addWidget(self._header_icon)
        self._title = QLabel(self._card)
        self._title.setMinimumHeight(20)
        header.addWidget(self._title)
        header.addStretch()
        self._cancel = QPushButton(self._card)
        self._cancel.setObjectName("ZoomCancel")
        self._cancel.setFixedHeight(20)
        self._cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel.clicked.connect(self.cancel_requested)
        self._cancel.hide()
        header.addWidget(self._cancel)
        self._card_layout.addLayout(header)
        self._header_divider = QFrame(self._card)
        self._header_divider.setFixedHeight(1)
        self._card_layout.addWidget(self._header_divider)

        actions = QVBoxLayout()
        actions.setContentsMargins(12, 12, 12, 12)
        actions.setSpacing(8)
        self._before = QPushButton(self._card)
        self._start = QPushButton(self._card)
        self._start.setObjectName("ZoomMeetingStart")
        self._after = QPushButton(self._card)
        self._combined = QPushButton(self._card)
        for button in (self._before, self._start, self._after, self._combined):
            button.setMinimumHeight(40)
            button.setIconSize(QSize(16, 16))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            install_themed_tooltip(button)
            actions.addWidget(button)
        self._before.clicked.connect(self.before_meeting_requested)
        self._start.clicked.connect(self.meeting_start_requested)
        self._after.clicked.connect(self.after_meeting_requested)
        self._combined.clicked.connect(self.before_meeting_requested)
        self._before.hide()
        self._after.hide()
        self._card_layout.addLayout(actions)

        self._integration = QWidget(self._card)
        self._integration.setStyleSheet("background:transparent;")
        self._integration_layout = QVBoxLayout(self._integration)
        self._integration_layout.setContentsMargins(0, 0, 0, 0)
        self._integration_layout.setSpacing(0)
        self._link_divider = QFrame(self._integration)
        self._link_divider.setFixedHeight(1)
        self._integration_layout.addWidget(self._link_divider)
        options = QHBoxLayout()
        options.setContentsMargins(16, 12, 16, 14)
        self._link = QCheckBox(self._integration)
        self._link.setCursor(Qt.CursorShape.PointingHandCursor)
        install_themed_tooltip(self._link)
        self._link.toggled.connect(self.background_song_link_toggled)
        options.addWidget(self._link, 1)
        self._integration_layout.addLayout(options)
        self._integration.hide()
        self._card_layout.addWidget(self._integration)
        self._root_layout.addWidget(self._card)
        self.setFixedWidth(340)
        self._retranslate()
        self.apply_theme()

    def _retranslate(self) -> None:
        self._title.setText(self.tr("Zoom Meeting"))
        self._before.setText(self.tr("Before meeting"))
        self._start.setText(self.tr("Start of meeting"))
        self._start.setToolTip(self.tr("Connect your audio and video and mute everyone."))
        self._after.setText(self.tr("End of meeting"))
        self._combined.setText(self.tr("Before/after meeting"))
        for button in (self._before, self._after, self._combined):
            button.setToolTip(
                self.tr("Disconnect your audio and video and allow everyone to unmute.")
            )
        self._link.setText(self.tr("Link background song"))
        self._link.setToolTip(
            self.tr(
                "Stop the song at meeting start; play it before/after the meeting. Scheduled song stops also prepare Zoom."
            )
        )
        self._cancel.setText(self.tr("Cancel"))
        self._refresh()

    def apply_theme(self) -> None:
        self._card.setStyleSheet(
            "QFrame#ZoomPanelCard {"
            f"background:{PALETTE.surface};"
            f"border:1px solid {PALETTE.border};border-radius:12px;}}"
            f"QLabel {{color:{PALETTE.text_primary};background:transparent;border:none;}}"
            f"QCheckBox {{color:{PALETTE.text_secondary};background:transparent;"
            "font-size:12px;spacing:8px;}"
            "QCheckBox::indicator {width:14px;height:14px;}"
            "QCheckBox::indicator:unchecked {"
            f"border:1px solid {PALETTE.border_strong};border-radius:4px;"
            f"background:{PALETTE.surface_alt};}}"
            f"QCheckBox::indicator:unchecked:hover {{border-color:{PALETTE.accent};}}"
            f"QCheckBox:disabled {{color:{PALETTE.text_dim};}}"
            "QPushButton {"
            f"color:{PALETTE.text_primary};background:{PALETTE.surface_alt};"
            f"border:1px solid {PALETTE.border_muted};"
            "border-radius:7px;padding:0 12px;font-size:12px;font-weight:500;text-align:left;}"
            f"QPushButton:hover {{background:{PALETTE.surface_hover};border-color:{PALETTE.border_strong};}}"
            f"QPushButton:pressed {{background:{PALETTE.surface_hover_strong};}}"
            "QPushButton#ZoomMeetingStart {"
            f"color:{PALETTE.accent_text};background:{PALETTE.accent_muted};"
            f"border-color:{qss_rgba(PALETTE.accent, 0.32)};}}"
            f"QPushButton#ZoomMeetingStart:hover {{background:{PALETTE.accent_muted_hover};}}"
            f"QPushButton#ZoomMeetingStart:pressed {{background:{PALETTE.accent_tint};}}"
            "QPushButton#ZoomCancel {"
            f"color:{PALETTE.text_secondary};background:transparent;border-color:transparent;"
            "padding:3px 6px;font-size:11px;text-align:center;}"
            f"QPushButton#ZoomCancel:hover {{background:{PALETTE.surface_hover};}}"
            "QPushButton:disabled,QPushButton#ZoomMeetingStart:disabled {"
            f"color:{PALETTE.text_dim};background:transparent;border-color:{PALETTE.border_muted};}}"
            f"QPushButton:focus {{border-color:{PALETTE.accent_alt};}}"
        )
        self._title.setStyleSheet("font-size:13px;font-weight:600;background:transparent;")
        self._header_icon.setPixmap(make_icon(ICON_ZOOM, 15, PALETTE.text_muted).pixmap(15, 15))
        self._start.setIcon(make_icon(ICON_VIDEO, 16, PALETTE.accent_text))
        for button in (self._before, self._after, self._combined):
            button.setIcon(make_icon(ICON_PEOPLE, 16, PALETTE.text_secondary))
        for divider in (self._header_divider, self._link_divider):
            divider.setStyleSheet(f"background:{PALETTE.border_muted};border:none;")
        self._sync_geometry()

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def set_split_meeting_actions(self, split: bool) -> None:
        self._before.setVisible(split)
        self._after.setVisible(split)
        self._combined.setVisible(not split)
        self._sync_geometry()

    def set_background_song_link(self, available: bool, linked: bool) -> None:
        self._integration.setVisible(available)
        previous = self._link.blockSignals(True)
        self._link.setChecked(linked)
        self._link.blockSignals(previous)
        self._sync_geometry()

    def _refresh(self) -> None:
        for button in (self._before, self._start, self._after, self._combined):
            button.setEnabled(self._connected and not self._busy)
        self._link.setEnabled(not self._busy)
        self._cancel.setVisible(self._busy)
        self.setToolTip(
            ""
            if self._connected
            else self.tr("No meeting found. Open a Zoom meeting to use these controls.")
        )
        self._sync_geometry()

    def _sync_geometry(self) -> None:
        self.ensurePolished()
        # Hidden child widgets defer layout propagation. Resolve their hints
        # from the inside out before sizing and positioning the popup.
        self._integration_layout.activate()
        self._card_layout.activate()
        self._root_layout.invalidate()
        self._root_layout.activate()
        self.adjustSize()
        if self._anchor_rect is None or self._available_rect is None:
            return
        anchor = self._anchor_rect
        screen = self._available_rect
        x = max(
            screen.left() + 4,
            min(
                anchor.x() + (anchor.width() - self.width()) // 2, screen.right() - self.width() - 4
            ),
        )
        y = anchor.y() - self.height() - 10
        if y < screen.top() + 4:
            y = anchor.y() + anchor.height() + 10
        y = max(screen.top() + 4, min(y, screen.bottom() - self.height() - 4))
        self.move(x, y)

    def show_above(self, anchor: QWidget) -> None:
        self._anchor_rect = QRect(anchor.mapToGlobal(anchor.rect().topLeft()), anchor.size())
        self._available_rect = anchor.screen().availableGeometry()
        self._sync_geometry()
        self._fade.stop()
        self._opacity.setOpacity(0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0)
        self._fade.setEndValue(1)
        self._fade.start()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate()
        super().changeEvent(event)
