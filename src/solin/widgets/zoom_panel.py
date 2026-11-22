"""Toolbar popup for Windows Zoom meeting and participant controls."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    QSignalBlocker,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLayout,
    QPushButton,
    QScrollArea,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from solin.core.integrations.automation.zoom.state import (
    AudioState,
    ParticipantAction,
    ParticipantActionResult,
    ParticipantStepStatus,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
)
from solin.styles.icons import (
    ICON_CHECK,
    ICON_INFO_CIRCLE,
    ICON_PEOPLE,
    ICON_PLAY,
    ICON_RAISED_HAND,
    ICON_RAISED_HAND_FILLED,
    ICON_SLIDERS,
    ICON_ZOOM,
    make_icon,
)
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip
from solin.widgets.zoom_panel_controls import (
    ZoomRaisedHandRow,
    ZoomSwitch,
    ZoomWorkflowButton,
    icon_label,
    plain_label,
    set_icon,
)


_PANEL_WIDTH = 462
_ROSTER_ROWS = 4


class ZoomPanel(QWidget):
    before_meeting_requested = Signal()
    meeting_start_requested = Signal()
    after_meeting_requested = Signal()
    background_song_link_toggled = Signal(bool)
    cancel_requested = Signal()
    raised_hands_toggled = Signal(bool)
    timer_integration_toggled = Signal(bool)
    manual_hand_toggled = Signal(bool)
    raised_hands_demo_toggled = Signal(bool)
    participant_action_requested = Signal(object, str)

    def __init__(
        self, parent=None, *, raised_hands_available: bool = False, hand_demo_available: bool = False
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._connected = False
        self._busy = False
        self._hands_snapshot = RaisedHandsSnapshot()
        self._hands_demo: RaisedHandsSnapshot | None = None
        self._participant_result: ParticipantActionResult | None = None
        self._participant_request_rejected = False
        self._rows: dict[str | tuple[int, str], ZoomRaisedHandRow] = {}
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._card = QFrame(self)
        self._card.setObjectName("ZoomPanelCard")
        self._root_layout.addWidget(self._card)
        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(15, 13, 15, 8)
        header.setSpacing(10)
        self._header_icon = icon_label(self._card, 32, "ZoomBrand")
        header.addWidget(self._header_icon)
        title = plain_label(self._card, "ZoomTitle")
        title.setText("Zoom")
        header.addWidget(title, 1)
        self._demo_toggle: QPushButton | None = None
        if raised_hands_available and hand_demo_available:
            self._demo_toggle = QPushButton(self._card)
            self._demo_toggle.setObjectName("ZoomQuietAction")
            self._demo_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            self._demo_toggle.setMinimumHeight(28)
            self._demo_toggle.setCheckable(True)
            self._demo_toggle.toggled.connect(self.raised_hands_demo_toggled)
            self._demo_toggle.hide()
            header.addWidget(self._demo_toggle)
        self._card_layout.addLayout(header)

        self._tabs = QTabBar(self._card)
        self._tabs.setObjectName("ZoomTabs")
        self._tabs.setDrawBase(False)
        self._tabs.setExpanding(False)
        self._tabs.setUsesScrollButtons(False)
        self._tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self._tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tabs.addTab("")
        self._hands_tab = self._tabs.addTab("")
        self._tabs.setTabVisible(self._hands_tab, raised_hands_available)
        self._count = plain_label(self._tabs, "ZoomHandCount")
        self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._count.hide()
        tabs_layout = QHBoxLayout()
        tabs_layout.setContentsMargins(15, 0, 15, 0)
        tabs_layout.addWidget(self._tabs, 1)
        self._manual_hand = QPushButton(self._card)
        self._manual_hand.setObjectName("ZoomManualHand")
        self._manual_hand.setFixedSize(33, 32)
        self._manual_hand.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_hand.setCheckable(True)
        self._manual_hand.setEnabled(False)
        self._manual_hand.toggled.connect(self.manual_hand_toggled)
        self._manual_hand.hide()
        install_themed_tooltip(self._manual_hand)
        tabs_layout.addWidget(self._manual_hand, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._card_layout.addLayout(tabs_layout)
        self._card_layout.addWidget(self._divider(self._card))
        self._tabs.setVisible(raised_hands_available)

        self._busy_strip = QWidget(self._card)
        self._busy_layout = busy_layout = QHBoxLayout(self._busy_strip)
        busy_layout.setContentsMargins(15, 4, 15, 4)
        self._busy_text = plain_label(self._busy_strip, "ZoomSecondary", wrap=True)
        busy_layout.addWidget(self._busy_text, 1)
        self._cancel = QPushButton(self._busy_strip)
        self._cancel.setObjectName("ZoomQuietAction")
        self._cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel.clicked.connect(self.cancel_requested)
        busy_layout.addWidget(self._cancel)
        self._card_layout.addWidget(self._busy_strip)

        self._build_meeting_page()
        self._build_hands_page()
        self._hands_page.hide()
        self._tabs.currentChanged.connect(self._show_tab)
        self.setFixedWidth(_PANEL_WIDTH)
        self._retranslate()
        self.apply_theme()

    @staticmethod
    def _divider(parent: QWidget) -> QFrame:
        line = QFrame(parent)
        line.setObjectName("ZoomDivider")
        line.setFixedHeight(1)
        return line

    def _build_meeting_page(self) -> None:
        self._meeting_page = QWidget(self._card)
        self._meeting_layout = actions = QVBoxLayout(self._meeting_page)
        actions.setContentsMargins(15, 13, 15, 12)
        actions.setSpacing(8)
        self._before = ZoomWorkflowButton(self._meeting_page)
        self._start = ZoomWorkflowButton(self._meeting_page, primary=True)
        self._after = ZoomWorkflowButton(self._meeting_page)
        self._combined = ZoomWorkflowButton(self._meeting_page)
        self._workflow_buttons = (self._before, self._start, self._after, self._combined)
        for button in self._workflow_buttons:
            actions.addWidget(button)
        self._before.clicked.connect(self.before_meeting_requested)
        self._start.clicked.connect(self.meeting_start_requested)
        self._after.clicked.connect(self.after_meeting_requested)
        self._combined.clicked.connect(self.before_meeting_requested)
        self._before.hide()
        self._after.hide()

        self._integration = QWidget(self._meeting_page)
        self._integration_layout = integration = QVBoxLayout(self._integration)
        integration.setContentsMargins(0, 4, 0, 0)
        integration.setSpacing(6)
        integration.addWidget(self._divider(self._integration))
        self._link = ZoomSwitch(self._integration, description=True)
        self._link.toggled.connect(self.background_song_link_toggled)
        integration.addWidget(self._link)
        self._integration.hide()
        actions.addWidget(self._integration)
        self._card_layout.addWidget(self._meeting_page)

    def _build_hands_page(self) -> None:
        self._hands_page = QWidget(self._card)
        self._hands_layout = hands = QVBoxLayout(self._hands_page)
        hands.setContentsMargins(15, 10, 15, 10)
        hands.setSpacing(0)

        self._hands_status = QFrame(self._hands_page)
        self._hands_status.setObjectName("ZoomHandsStatus")
        self._status_layout = status_layout = QHBoxLayout(self._hands_status)
        status_layout.setContentsMargins(11, 10, 11, 10)
        status_layout.setSpacing(8)
        self._hands_status_icon = icon_label(self._hands_status, 16)
        self._hands_status_title = plain_label(self._hands_status, "ZoomItemTitle", wrap=True)
        self._hands_status_text = plain_label(self._hands_status, "ZoomSecondary", wrap=True)
        status_layout.addWidget(self._hands_status_icon, alignment=Qt.AlignmentFlag.AlignTop)
        status_copy = QVBoxLayout()
        status_copy.setSpacing(2)
        status_copy.addWidget(self._hands_status_title)
        status_copy.addWidget(self._hands_status_text)
        status_layout.addLayout(status_copy, 1)
        hands.addWidget(self._hands_status)

        self._roster = QScrollArea(self._hands_page)
        self._roster.setObjectName("ZoomRoster")
        self._roster.setFrameShape(QFrame.Shape.NoFrame)
        self._roster.setWidgetResizable(True)
        self._roster.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._roster.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._roster_body = QWidget(self._roster)
        self._roster_body.setObjectName("ZoomRosterBody")
        self._roster_layout = QVBoxLayout(self._roster_body)
        self._roster_layout.setContentsMargins(0, 0, 0, 0)
        self._roster_layout.setSpacing(0)
        self._roster_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._roster.setWidget(self._roster_body)
        hands.addWidget(self._roster)

        self._feedback_line = QWidget(self._hands_page)
        self._feedback_line.setObjectName("ZoomFeedback")
        self._feedback_layout = feedback = QHBoxLayout(self._feedback_line)
        feedback.setContentsMargins(0, 7, 0, 0)
        feedback.setSpacing(7)
        self._feedback_icon = icon_label(self._feedback_line, 16)
        self._hands_feedback = plain_label(self._feedback_line, "ZoomSecondary", wrap=True)
        feedback.addWidget(self._feedback_icon)
        feedback.addWidget(self._hands_feedback, 1)
        hands.addWidget(self._feedback_line)
        hands.addWidget(self._build_hands_footer())
        self._card_layout.addWidget(self._hands_page)

    def _build_hands_footer(self) -> QWidget:
        footer = QWidget(self._hands_page)
        self._hands_footer_layout = layout = QVBoxLayout(footer)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._divider(footer))
        self._monitor_hands = ZoomSwitch(footer)
        self._monitor_hands.toggled.connect(self.raised_hands_toggled)
        layout.addWidget(self._monitor_hands)
        self._timer_integration = ZoomSwitch(footer, description=True)
        self._timer_integration.toggled.connect(self.timer_integration_toggled)
        layout.addWidget(self._timer_integration)
        self._timer_integration.hide()
        return footer

    def _show_tab(self, index: int) -> None:
        hands = index == self._hands_tab
        self._meeting_page.setVisible(not hands)
        self._hands_page.setVisible(hands)
        self._manual_hand.setVisible(hands and not self._timer_integration.isHidden())
        if self._demo_toggle is not None:
            self._demo_toggle.setVisible(hands)
        self._update_tab_icons()
        self._sync_geometry()

    def _retranslate(self) -> None:
        self._tabs.setTabText(0, self.tr("Meeting"))
        self._tabs.setTabText(self._hands_tab, self.tr("Raised hands"))
        self._roster.setAccessibleName(self.tr("Raised hands"))
        self._start.set_copy(
            self.tr("Start of meeting"), self.tr("Connect your audio and video and mute everyone.")
        )
        meeting_audio = self.tr("Disconnect your audio and video and ask everyone to unmute.")
        self._before.set_copy(self.tr("Before meeting"), meeting_audio)
        self._after.set_copy(self.tr("End of meeting"), meeting_audio)
        self._combined.set_copy(self.tr("Before/after meeting"), meeting_audio)
        self._link.set_copy(
            self.tr("Link background song"), self.tr("Sync playback with meeting controls.")
        )
        self._link.setToolTip(
            self.tr(
                "Stop the song at meeting start; play it before/after the meeting. Scheduled song stops also prepare Zoom."
            )
        )
        self._busy_text.setText(self.tr("Automation in progress…"))
        self._cancel.setText(self.tr("Cancel"))
        self._monitor_hands.set_copy(self.tr("Monitor raised hands"))
        manual_label = self.tr("Show hand on timer")
        self._manual_hand.setAccessibleName(manual_label)
        self._manual_hand.setToolTip(manual_label)
        if self._demo_toggle is not None:
            self._demo_toggle.setText(self.tr("Test raised hands"))
        self._timer_integration.set_copy(
            self.tr("Integrate with timer"),
            self.tr("Raised hands in Zoom will appear on the timer display."),
        )
        self._action_labels = {
            ParticipantAction.LOWER_HAND: self.tr("Lower hand"),
            ParticipantAction.REQUEST_UNMUTE: self.tr("Ask to unmute"),
            ParticipantAction.MUTE_AND_LOWER: self.tr("Mute and lower hand"),
        }
        self._unmute_requested_label = self.tr("Unmute requested.")
        self._audio_labels = {
            AudioState.MUTED: self.tr("Microphone muted"),
            AudioState.UNMUTED: self.tr("Microphone on"),
            AudioState.DISCONNECTED: self.tr("Audio disconnected"),
            AudioState.UNKNOWN: self.tr("Microphone status unknown"),
        }
        for row in self._rows.values():
            self._translate_row(row)
        self._refresh()

    def _translate_row(self, row: ZoomRaisedHandRow) -> None:
        row.set_copy(
            self._action_labels, self._unmute_requested_label, self._audio_labels[row.hand.audio]
        )

    def apply_theme(self) -> None:
        p = PALETTE
        self._card.setStyleSheet(f"""
            QWidget {{background: transparent;}}
            QFrame#ZoomPanelCard {{
                background: {p.surface}; border: 1px solid {p.border}; border-radius: 14px;
            }}
            QScrollArea#ZoomRoster {{border: none;}}
            QLabel {{color: {p.text_primary}; border: none; background: transparent;}}
            QLabel#ZoomTitle {{font-size: 17px; font-weight: 650;}}
            QLabel#ZoomBrand, QLabel#ZoomHandCount {{background: {p.accent_muted};}}
            QLabel#ZoomBrand {{border-radius: 9px;}}
            QLabel#ZoomHandCount {{
                color: {p.accent_text}; border-radius: 6px; padding: 1px 5px;
                min-width: 10px; min-height: 18px; font-size: 11px; font-weight: 600;
            }}
            QFrame#ZoomDivider {{background: {p.border_muted}; border: none;}}
            QTabBar#ZoomTabs::tab {{
                color: {p.text_secondary}; background: transparent; border: none;
                border-bottom: 2px solid transparent; padding: 0 2px;
                min-height: 37px; font-size: 13px; font-weight: 600;
            }}
            QTabBar#ZoomTabs::tab:first {{margin-right: 14px;}}
            QTabBar#ZoomTabs::tab:selected {{
                color: {p.accent_text}; border-bottom-color: {p.accent_alt};
            }}
            QTabBar#ZoomTabs::tab:hover {{color: {p.accent_text};}}
            QTabBar#ZoomTabs::tab:focus {{background: {p.surface_hover};}}
            QLabel#ZoomItemTitle {{font-size: 13px; font-weight: 600;}}
            QLabel#ZoomSecondary {{font-size: 12px; color: {p.text_secondary};}}
            QLabel#ZoomAudio {{font-size: 11px; color: {p.text_secondary};}}
            QLabel#ZoomItemTitle:disabled, QLabel#ZoomSecondary:disabled {{color: {p.text_muted};}}
            QPushButton#ZoomWorkflow {{
                background: transparent; border: 1px solid {p.border_muted}; border-radius: 9px;
            }}
            QLabel#ZoomWorkflowIcon {{
                background: {p.surface_hover}; border-radius: 7px;
            }}
            QPushButton#ZoomWorkflow[primary="true"] {{
                background: {p.accent_muted}; border-color: {qss_rgba(p.accent_alt, 0.32)};
            }}
            QLabel#ZoomWorkflowIcon[primary="true"] {{background: {p.surface};}}
            QPushButton#ZoomWorkflow:hover {{
                background: {p.surface_hover}; border-color: {p.border_strong};
            }}
            QPushButton#ZoomWorkflow[primary="true"]:hover {{background: {p.accent_muted_hover};}}
            QPushButton#ZoomWorkflow:pressed {{background: {p.surface_hover_strong};}}
            QPushButton#ZoomWorkflow:disabled {{background: transparent; border-color: {p.border_muted};}}
            QPushButton#ZoomAudioAction, QPushButton#ZoomQuietAction {{
                font-size: 12px; font-weight: 600; padding: 0 7px;
                border: 1px solid transparent; border-radius: 7px;
            }}
            QPushButton#ZoomAudioAction {{
                background: {p.accent_muted}; border-color: {qss_rgba(p.accent_alt, 0.32)};
                color: {p.accent_text};
            }}
            QPushButton#ZoomAudioAction:hover {{background: {p.accent_muted_hover};}}
            QPushButton#ZoomQuietAction {{background: transparent; color: {p.text_secondary};}}
            QPushButton#ZoomQuietAction:hover {{background: {p.surface_hover}; color: {p.text_primary};}}
            QPushButton#ZoomQuietAction:checked {{
                background: {p.accent_muted}; border-color: {qss_rgba(p.accent_alt, 0.32)};
                color: {p.accent_text};
            }}
            QPushButton#ZoomAudioAction:pressed, QPushButton#ZoomQuietAction:pressed {{
                background: {p.surface_hover_strong};
            }}
            QPushButton#ZoomAudioAction:disabled, QPushButton#ZoomQuietAction:disabled {{
                color: {p.text_dim}; background: transparent; border-color: transparent;
            }}
            QPushButton#ZoomWorkflow:focus, QPushButton#ZoomAudioAction:focus,
            QPushButton#ZoomQuietAction:focus {{border-color: {p.accent_alt};}}
            QPushButton#ZoomManualHand {{
                background: {p.surface_hover}; border: 1px solid {p.border_muted};
                border-radius: 8px;
            }}
            QPushButton#ZoomManualHand:hover {{border-color: {p.border_strong};}}
            QPushButton#ZoomManualHand:checked {{
                background: {qss_rgba(p.warning_text, 0.14)};
                border-color: {qss_rgba(p.warning_text, 0.40)};
            }}
            QPushButton#ZoomManualHand:disabled {{background: transparent;}}
            QPushButton#ZoomManualHand:focus {{border-color: {p.accent_alt};}}
            QFrame#ZoomHandRow {{border: none; border-top: 1px solid {p.border_muted};}}
            QFrame#ZoomHandsStatus {{background: {p.surface_hover}; border: none; border-radius: 8px;}}
            QWidget#ZoomFeedback {{border-top: 1px solid {p.border_muted};}}
        """)
        set_icon(self._header_icon, ICON_ZOOM, 17, p.accent_text)
        self._update_manual_hand_icon()
        self._start.apply_theme(ICON_PLAY, p.accent_text)
        for button in (self._before, self._after, self._combined):
            button.apply_theme(ICON_PEOPLE, p.text_secondary)
        for row in self._rows.values():
            row.apply_theme()
        self._update_tab_icons()
        self._refresh()
        self.update()

    def _update_tab_icons(self) -> None:
        for index, icon in ((0, ICON_SLIDERS), (self._hands_tab, ICON_RAISED_HAND)):
            color = (
                PALETTE.accent_text
                if self._tabs.currentIndex() == index
                else PALETTE.text_secondary
            )
            self._tabs.setTabIcon(index, make_icon(icon, 16, color))

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
        with QSignalBlocker(self._link):
            self._link.setChecked(linked)
        self._sync_geometry()

    def set_timer_hand_controls(self, *, available: bool, integrated: bool, manual: bool) -> None:
        self._timer_integration.setVisible(available)
        self._manual_hand.setVisible(available and self._tabs.currentIndex() == self._hands_tab)
        self._manual_hand.setEnabled(available and integrated)
        with QSignalBlocker(self._timer_integration), QSignalBlocker(self._manual_hand):
            self._timer_integration.setChecked(integrated)
            self._manual_hand.setChecked(manual)
        self._update_manual_hand_icon()
        self._sync_geometry()

    def _update_manual_hand_icon(self) -> None:
        checked = self._manual_hand.isChecked()
        self._manual_hand.setIcon(
            make_icon(
                ICON_RAISED_HAND_FILLED if checked else ICON_RAISED_HAND,
                17,
                PALETTE.warning_text if checked else PALETTE.text_secondary,
            )
        )

    def set_raised_hands_demo(self, snapshot: RaisedHandsSnapshot | None) -> None:
        if self._demo_toggle is None or snapshot == self._hands_demo:
            return
        self._hands_demo = snapshot
        with QSignalBlocker(self._demo_toggle):
            self._demo_toggle.setChecked(snapshot is not None)
        self._sync_roster()
        self._refresh_raised_hands()
        self._sync_geometry()

    def set_raised_hands_snapshot(self, snapshot: RaisedHandsSnapshot) -> None:
        if snapshot != self._hands_snapshot:
            self._hands_snapshot = snapshot
            if snapshot.status is RaisedHandsStatus.INACTIVE:
                self._clear_participant_feedback()
            if self._hands_demo is None:
                self._sync_roster()
        self._refresh()

    def _sync_roster(self) -> None:
        snapshot = self._hands_demo or self._hands_snapshot
        hands = (
            snapshot.hands
            if snapshot.status in (RaisedHandsStatus.READY, RaisedHandsStatus.PARTIAL)
            else ()
        )
        previous_keys = tuple(self._rows)
        rows = {}
        for index, hand in enumerate(hands):
            key = hand.reference or (index, hand.name)
            row = self._rows.pop(key, None)
            if row is None:
                row = ZoomRaisedHandRow(hand, self._roster_body)
                row.requested.connect(self._request_participant_action)
                self._translate_row(row)
                row.apply_theme()
            if row.hand != hand:
                row.set_hand(hand, self._audio_labels[hand.audio])
            rows[key] = row
        focus = QApplication.focusWidget()
        restore_focus = focus is not None and any(
            row.isAncestorOf(focus) for row in self._rows.values()
        )
        for row in self._rows.values():
            row.set_actions_enabled(False)
            row.hide()
            row.deleteLater()
        if previous_keys != tuple(rows):
            while self._roster_layout.count():
                self._roster_layout.takeAt(self._roster_layout.count() - 1)
            previous_control: QWidget = self._manual_hand
            for row in rows.values():
                self._roster_layout.addWidget(row)
                for button in row.tab_order():
                    QWidget.setTabOrder(previous_control, button)
                    previous_control = button
            QWidget.setTabOrder(previous_control, self._monitor_hands)
        self._rows = rows
        if restore_focus:
            self._monitor_hands.setFocus()

    def _request_participant_action(self, action: ParticipantAction, reference: str) -> None:
        self._clear_participant_feedback()
        self._refresh()
        self.participant_action_requested.emit(action, reference)

    def set_participant_action_result(self, result: ParticipantActionResult) -> None:
        if self._hands_snapshot.status is RaisedHandsStatus.INACTIVE:
            return
        self._participant_result = result
        self._participant_request_rejected = False
        row = self._rows.get(result.reference)
        if row is not None and result.ok and self._unmute_was_requested(result):
            row.acknowledge_unmute()
        self._refresh()

    def reject_participant_action(self) -> None:
        self._participant_result = None
        self._participant_request_rejected = True
        self._refresh()

    def _clear_participant_feedback(self) -> None:
        self._participant_result = None
        self._participant_request_rejected = False

    def _raised_hands_status(self) -> tuple[str, str]:
        if self._hands_demo is not None:
            return "", ""
        status = self._hands_snapshot.status
        if status is RaisedHandsStatus.NO_MEETING or (
            status is RaisedHandsStatus.INACTIVE and not self._connected
        ):
            return self.tr("Waiting for a Zoom meeting."), ""
        if status is RaisedHandsStatus.INACTIVE:
            return (
                self.tr("Monitoring is off"),
                self.tr("Enable monitoring to see raised hands in Zoom."),
            )
        if status is RaisedHandsStatus.PANEL_CLOSED:
            return self.tr("Monitoring paused"), self.tr("Open Participants in Zoom to resume.")
        if status is RaisedHandsStatus.UNAVAILABLE:
            if self._hands_snapshot.error:
                return self.tr("List unavailable"), self.tr("Could not read the Participants list.")
            return self.tr("Checking Zoom Participants…"), ""
        if status is RaisedHandsStatus.PARTIAL:
            return self.tr("Only part of the Participants list is available."), ""
        if self._rows:
            return "", ""
        return (
            self.tr("No raised hands."),
            self.tr("New raised hands appear here during the meeting."),
        )

    @staticmethod
    def _unmute_was_requested(result: ParticipantActionResult) -> bool:
        return any(
            step.status is ParticipantStepStatus.REQUESTED for step in result.completed_steps
        )

    def _participant_feedback(self) -> str:
        result = self._participant_result
        if self._participant_request_rejected:
            return self.tr("Participant action could not be completed.")
        if result is None:
            return ""
        if not result.ok:
            if (
                result.action is ParticipantAction.MUTE_AND_LOWER
                and len(result.completed_steps) == 1
            ):
                return self.tr("Participant muted. Could not confirm the hand was lowered.")
            return self.tr("Participant action could not be completed.")
        if self._unmute_was_requested(result):
            return self._unmute_requested_label
        if result.action is ParticipantAction.LOWER_HAND:
            return self.tr("Hand lowered.")
        if result.action is ParticipantAction.MUTE_AND_LOWER:
            return self.tr("Participant muted and hand lowered.")
        return ""

    def _refresh_raised_hands(self) -> None:
        active = self._hands_snapshot.status is not RaisedHandsStatus.INACTIVE
        with QSignalBlocker(self._monitor_hands):
            self._monitor_hands.setChecked(active)
        self._monitor_hands.setEnabled((active or self._connected) and not self._busy)
        count = (
            len(self._rows)
            if self._hands_demo is not None or self._hands_snapshot.status is RaisedHandsStatus.READY
            else 0
        )
        side = QTabBar.ButtonPosition.RightSide
        if count:
            text = str(count)
            if self._count.text() != text or self._count.isHidden():
                self._tabs.setTabButton(self._hands_tab, side, None)
                self._count.setText(text)
                self._count.ensurePolished()
                self._count.adjustSize()
                self._tabs.setTabButton(self._hands_tab, side, self._count)
        elif not self._count.isHidden():
            self._tabs.setTabButton(self._hands_tab, side, None)
        tab_text = self._tabs.tabText(self._hands_tab)
        self._tabs.setAccessibleTabName(
            self._hands_tab, f"{tab_text} ({count})" if count else tab_text
        )

        title, description = self._raised_hands_status()
        self._hands_status_title.setText(title)
        self._hands_status_text.setText(description)
        self._hands_status_text.setVisible(bool(description))
        self._hands_status.setVisible(bool(title))
        if title:
            warning = (
                self._hands_snapshot.status
                in (RaisedHandsStatus.PANEL_CLOSED, RaisedHandsStatus.PARTIAL)
                or self._hands_snapshot.error is not None
            )
            set_icon(
                self._hands_status_icon,
                ICON_INFO_CIRCLE if warning else ICON_RAISED_HAND,
                16,
                PALETTE.warning_text if warning else PALETTE.accent_text,
            )
        can_act = (
            self._connected
            and not self._busy
            and self._hands_demo is None
            and self._hands_snapshot.status is RaisedHandsStatus.READY
        )
        for row in self._rows.values():
            row.set_actions_enabled(can_act)
        self._roster.setVisible(bool(self._rows))

        feedback = self._participant_feedback() if self._hands_demo is None else ""
        self._hands_feedback.setText(feedback)
        self._feedback_line.setVisible(bool(feedback))
        if feedback:
            success = self._participant_result is not None and self._participant_result.ok
            set_icon(
                self._feedback_icon,
                ICON_CHECK if success else ICON_INFO_CIRCLE,
                15,
                PALETTE.success if success else PALETTE.warning_text,
            )

    def _refresh(self) -> None:
        for button in self._workflow_buttons:
            button.setEnabled(self._connected and not self._busy)
        self._link.setEnabled(not self._busy)
        self._refresh_raised_hands()
        self._busy_strip.setVisible(self._busy)
        self._cancel.setVisible(self._busy)
        self.setToolTip(
            ""
            if self._connected
            else self.tr("No meeting found. Open a Zoom meeting to use these controls.")
        )
        self._sync_geometry()

    def _fit_roster(self) -> None:
        if not self._rows:
            return
        sample = next(iter(self._rows.values()))
        button = sample.button(ParticipantAction.REQUEST_UNMUTE)
        button.ensurePolished()
        metrics = button.fontMetrics()
        audio_width = max(
            148,
            max(
                metrics.horizontalAdvance(self._action_labels[action])
                for action in self._action_labels
                if action is not ParticipantAction.LOWER_HAND
            )
            + 16,
            metrics.horizontalAdvance(self._unmute_requested_label) + 16,
        )
        lower_width = (
            metrics.horizontalAdvance(self._action_labels[ParticipantAction.LOWER_HAND]) + 16
        )
        width = self.width() - self._hands_layout.contentsMargins().left() * 2 - 2
        cap = self._available_rect.height() // 3 if self._available_rect else 220
        width -= self._roster.verticalScrollBar().sizeHint().width()
        for row in self._rows.values():
            row.fit(width, audio_width, lower_width)
        visible_rows = min(len(self._rows), _ROSTER_ROWS, max(1, cap // sample.height()))
        self._roster.setFixedHeight(visible_rows * sample.height())

    def _sync_geometry(self) -> None:
        self.ensurePolished()
        content_width = self.width() - 32
        for button in self._workflow_buttons:
            button.fit(content_width)
        self._link.fit(content_width)
        self._monitor_hands.fit(content_width)
        self._timer_integration.fit(content_width)
        self._hands_status.setFixedHeight(self._status_layout.totalHeightForWidth(content_width))
        self._feedback_line.setFixedHeight(self._feedback_layout.totalHeightForWidth(content_width))
        self._busy_strip.setFixedHeight(self._busy_layout.totalHeightForWidth(self.width() - 2))
        self._fit_roster()
        anchor = self._anchor_rect
        screen = self._available_rect
        # Hidden pages defer layout propagation, so measure the active page explicitly.
        for layout in (
            self._integration_layout,
            self._meeting_layout,
            self._hands_footer_layout,
            self._hands_layout,
            self._card_layout,
        ):
            layout.invalidate()
            layout.activate()
        self._root_layout.invalidate()
        height = self._root_layout.sizeHint().height()
        if anchor is None or screen is None:
            self.resize(self.width(), height)
            return

        x = max(
            screen.left() + 4,
            min(
                anchor.x() + (anchor.width() - self.width()) // 2, screen.right() - self.width() - 4
            ),
        )
        y = anchor.top() - height - 10
        if y < screen.top() + 4:
            y = anchor.bottom() + 11
        y = max(screen.top() + 4, min(y, screen.bottom() - height - 4))
        self.setGeometry(x, y, self.width(), height)

    def show_above(self, anchor: QWidget) -> None:
        self._anchor_rect = QRect(anchor.mapToGlobal(anchor.rect().topLeft()), anchor.size())
        self._available_rect = anchor.screen().availableGeometry()
        self.setFixedWidth(min(_PANEL_WIDTH, self._available_rect.width() - 8))
        self._sync_geometry()
        self._fade.stop()
        self._opacity.setOpacity(0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0)
        self._fade.setEndValue(1)
        self._fade.start()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate()
        super().changeEvent(event)
