from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from solin.core.remote_control.security import RemoteSessionInfo
from solin.styles.icons import ICON_REMOTE_CONTROL, make_icon
from solin.styles.theme import PALETTE, qss_rgba


class _RemoteSessionRow(QFrame):
    disconnect_requested = Signal(str)

    def __init__(self, session: RemoteSessionInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("RemoteSessionRow")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 10, 10)
        root.setSpacing(7)

        summary = QHBoxLayout()
        summary.setSpacing(10)
        device = QLabel(self._session_label())
        device.setObjectName("RemoteSessionDevice")
        device.setMinimumWidth(0)
        summary.addWidget(device, 1)

        state = QLabel(
            self.tr("Connected now") if session.connected_socket_count > 0 else self.tr("Signed in")
        )
        state.setObjectName(
            "RemoteSessionConnected"
            if session.connected_socket_count > 0
            else "RemoteSessionSignedIn"
        )
        summary.addWidget(state)
        root.addLayout(summary)

        metadata = QHBoxLayout()
        metadata.setSpacing(8)
        address = QLabel(session.remote_address or self.tr("Local network"))
        address.setObjectName("RemoteSessionMetadata")
        metadata.addWidget(address)
        self._activity = QLabel()
        self._activity.setObjectName("RemoteSessionMetadata")
        metadata.addWidget(self._activity)
        metadata.addStretch()

        self._disconnect = QPushButton(self.tr("Disconnect"))
        self._disconnect.setObjectName("RemoteSessionDisconnect")
        self._disconnect.setCursor(Qt.CursorShape.PointingHandCursor)
        self._disconnect.clicked.connect(self._show_confirmation)
        metadata.addWidget(self._disconnect)
        root.addLayout(metadata)

        self._confirmation = QWidget()
        confirmation_layout = QHBoxLayout(self._confirmation)
        confirmation_layout.setContentsMargins(0, 1, 0, 0)
        confirmation_layout.setSpacing(7)
        prompt = QLabel(self.tr("Disconnect this device?"))
        prompt.setObjectName("RemoteSessionPrompt")
        confirmation_layout.addWidget(prompt, 1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("RemoteSessionCancel")
        cancel.clicked.connect(self._cancel_confirmation)
        confirmation_layout.addWidget(cancel)
        confirm = QPushButton(self.tr("Disconnect"))
        confirm.setObjectName("RemoteSessionConfirm")
        confirm.setProperty("destructive", True)
        confirm.clicked.connect(self._confirm_disconnect)
        confirmation_layout.addWidget(confirm)
        self._confirmation.hide()
        root.addWidget(self._confirmation)

        self._feedback = QLabel()
        self._feedback.setObjectName("RemoteSessionError")
        self._feedback.setWordWrap(True)
        self._feedback.hide()
        root.addWidget(self._feedback)
        self.refresh_activity()

    def refresh_activity(self) -> None:
        self._activity.setText("·  " + self._relative_activity())

    def set_pending(self, pending: bool) -> None:
        self._disconnect.setEnabled(not pending)
        if pending:
            self._confirmation.hide()
            self._feedback.setText(self.tr("Disconnecting…"))
            self._feedback.setProperty("error", False)
            self._feedback.show()
        elif not bool(self._feedback.property("error")):
            self._feedback.hide()

    def show_error(self) -> None:
        self._disconnect.setEnabled(True)
        self._feedback.setProperty("error", True)
        self._feedback.setText(self.tr("Could not disconnect this device. Try again."))
        self._feedback.show()
        self.style().unpolish(self._feedback)
        self.style().polish(self._feedback)

    def _show_confirmation(self) -> None:
        self._disconnect.hide()
        self._feedback.hide()
        self._confirmation.show()

    def _cancel_confirmation(self) -> None:
        self._confirmation.hide()
        self._disconnect.show()

    def _confirm_disconnect(self) -> None:
        self._disconnect.show()
        self.set_pending(True)
        self.disconnect_requested.emit(self.session.management_id)

    def _session_label(self) -> str:
        browser = self.session.browser or self.tr("Remote device")
        platform = self.session.platform
        if browser and platform:
            label = self.tr("%1 on %2").replace("%1", browser).replace("%2", platform)
        else:
            label = browser or platform or self.tr("Remote device")
        if self.session.client_mode == "standalone":
            return self.tr("Solin app · %1").replace("%1", label)
        return label

    def _relative_activity(self) -> str:
        timestamp = self.session.last_activity_at_utc
        if timestamp <= 0:
            return self.tr("Activity unknown")
        elapsed = max(0, int(datetime.now(UTC).timestamp() - timestamp))
        if elapsed < 60:
            return self.tr("Active now")
        minutes = elapsed // 60
        if minutes < 60:
            return self.tr("Active %1 min ago").replace("%1", str(minutes))
        hours = minutes // 60
        if hours < 24:
            return self.tr("Active %1 h ago").replace("%1", str(hours))
        days = hours // 24
        return self.tr("Active %1 d ago").replace("%1", str(days))


class RemoteSessionsPopup(QWidget):
    """Operational session inventory opened from the floating toolbar."""

    disconnect_requested = Signal(str)
    disconnect_all_requested = Signal()

    _WIDTH = 352
    _MAX_LIST_HEIGHT = 276

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._WIDTH)

        self._enabled = False
        self._running = False
        self._runtime_message = ""
        self._sessions: tuple[RemoteSessionInfo, ...] = ()
        self._rows: dict[str, _RemoteSessionRow] = {}
        self._pending_ids: set[str] = set()
        self._failed_ids: set[str] = set()
        self._disconnect_all_pending = False

        opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(opacity)
        self._opacity = opacity
        self._fade = QPropertyAnimation(opacity, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(30_000)
        self._activity_timer.timeout.connect(self._refresh_activity_labels)

        self._build_ui()
        self.apply_theme()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("RemoteSessionsCard")
        card = QVBoxLayout(self._card)
        card.setContentsMargins(16, 15, 16, 14)
        card.setSpacing(11)

        header = QHBoxLayout()
        header.setSpacing(9)
        self._header_icon = QLabel()
        self._header_icon.setFixedSize(17, 17)
        header.addWidget(self._header_icon)
        title_copy = QVBoxLayout()
        title_copy.setSpacing(0)
        self._title = QLabel(self.tr("Remote control"))
        self._title.setObjectName("RemoteSessionsTitle")
        self._summary = QLabel()
        self._summary.setObjectName("RemoteSessionsSummary")
        title_copy.addWidget(self._title)
        title_copy.addWidget(self._summary)
        header.addLayout(title_copy, 1)
        self._count = QLabel()
        self._count.setObjectName("RemoteSessionsCount")
        self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._count)
        card.addLayout(header)

        self._runtime = QLabel()
        self._runtime.setObjectName("RemoteSessionsRuntime")
        self._runtime.setWordWrap(True)
        self._runtime.hide()
        card.addWidget(self._runtime)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("RemoteSessionsScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._scroll.setWidget(self._list)
        card.addWidget(self._scroll)

        self._empty = QLabel()
        self._empty.setObjectName("RemoteSessionsEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        card.addWidget(self._empty)

        self._footer = QWidget()
        footer_layout = QVBoxLayout(self._footer)
        footer_layout.setContentsMargins(0, 2, 0, 0)
        footer_layout.setSpacing(7)
        self._disconnect_all = QPushButton(self.tr("Disconnect all devices"))
        self._disconnect_all.setObjectName("RemoteSessionsDisconnectAll")
        self._disconnect_all.setProperty("destructive", True)
        self._disconnect_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._disconnect_all.clicked.connect(self._show_disconnect_all_confirmation)
        footer_layout.addWidget(self._disconnect_all)

        self._disconnect_all_confirmation = QWidget()
        confirm_layout = QVBoxLayout(self._disconnect_all_confirmation)
        confirm_layout.setContentsMargins(0, 0, 0, 0)
        confirm_layout.setSpacing(7)
        self._disconnect_all_prompt = QLabel(
            self.tr(
                "Disconnect every device? They will need to sign in again. "
                "Projection will continue."
            )
        )
        self._disconnect_all_prompt.setObjectName("RemoteSessionsPrompt")
        self._disconnect_all_prompt.setWordWrap(True)
        confirm_layout.addWidget(self._disconnect_all_prompt)
        actions = QHBoxLayout()
        actions.addStretch()
        self._disconnect_all_cancel = QPushButton(self.tr("Cancel"))
        self._disconnect_all_cancel.clicked.connect(self._cancel_disconnect_all)
        actions.addWidget(self._disconnect_all_cancel)
        self._disconnect_all_confirm = QPushButton(self.tr("Disconnect all"))
        self._disconnect_all_confirm.setProperty("destructive", True)
        self._disconnect_all_confirm.clicked.connect(self._confirm_disconnect_all)
        actions.addWidget(self._disconnect_all_confirm)
        confirm_layout.addLayout(actions)
        self._disconnect_all_confirmation.hide()
        footer_layout.addWidget(self._disconnect_all_confirmation)

        self._global_feedback = QLabel()
        self._global_feedback.setObjectName("RemoteSessionError")
        self._global_feedback.setWordWrap(True)
        self._global_feedback.hide()
        footer_layout.addWidget(self._global_feedback)
        card.addWidget(self._footer)

        root.addWidget(self._card)
        self._render()

    def set_runtime_state(self, enabled: bool, running: bool, message: str) -> None:
        self._enabled = bool(enabled)
        self._running = bool(running)
        self._runtime_message = str(message or "")
        self._render()

    def set_sessions(self, sessions: object) -> None:
        values = (
            tuple(session for session in sessions if isinstance(session, RemoteSessionInfo))
            if isinstance(sessions, (tuple, list))
            else ()
        )
        self._sessions = values
        active_ids = {session.management_id for session in values}
        self._pending_ids.intersection_update(active_ids)
        self._failed_ids.intersection_update(active_ids)
        if not values:
            self._disconnect_all_pending = False
        self._render()

    def set_revocation_result(self, management_id: str, succeeded: bool) -> None:
        if management_id:
            self._pending_ids.discard(management_id)
            self._failed_ids.discard(management_id)
            row = self._rows.get(management_id)
            if row is not None:
                if succeeded:
                    row.set_pending(True)
                else:
                    self._failed_ids.add(management_id)
                    row.show_error()
            return
        self._disconnect_all_pending = False
        if not succeeded:
            self._global_feedback.setText(self.tr("Could not disconnect all devices. Try again."))
            self._global_feedback.show()
        else:
            self._global_feedback.hide()
        self._render_footer()

    def show_above(self, anchor: QWidget) -> None:
        self._render()
        self.adjustSize()
        anchor_rect = anchor.rect()
        anchor_center = anchor.mapToGlobal(anchor_rect.center())
        screen = QGuiApplication.screenAt(anchor_center) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        x = anchor_center.x() - self.width() // 2
        y = anchor.mapToGlobal(anchor_rect.topLeft()).y() - self.height() - 10
        if available is not None:
            x = max(available.left() + 8, min(x, available.right() - self.width() - 8))
            if y < available.top() + 8:
                y = anchor.mapToGlobal(anchor_rect.bottomLeft()).y() + 10
        self.move(x, y)
        self._opacity.setOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def apply_theme(self) -> None:
        self._header_icon.setPixmap(
            make_icon(ICON_REMOTE_CONTROL, 17, PALETTE.accent).pixmap(17, 17)
        )
        self.setStyleSheet(
            f"""
            QFrame#RemoteSessionsCard {{
                background: {qss_rgba(PALETTE.surface_overlay, 0.98)};
                border: 1px solid {PALETTE.border}; border-radius: 16px;
            }}
            QWidget {{ background: transparent; color: {PALETTE.text_secondary}; }}
            QLabel#RemoteSessionsTitle {{ color: {PALETTE.text_primary}; font-size: 13px; font-weight: 700; }}
            QLabel#RemoteSessionsSummary {{ color: {PALETTE.text_muted}; font-size: 10px; }}
            QLabel#RemoteSessionsCount {{
                min-width: 22px; min-height: 22px; color: {PALETTE.accent_text_hover};
                background: {PALETTE.accent_muted}; border-radius: 11px;
                font-size: 10px; font-weight: 700;
            }}
            QLabel#RemoteSessionsRuntime {{
                color: {PALETTE.warning_text}; background: {PALETTE.warning_surface};
                border: 1px solid {PALETTE.warning_border}; border-radius: 8px;
                padding: 8px 9px; font-size: 10px;
            }}
            QFrame#RemoteSessionRow {{
                background: {PALETTE.surface_card}; border: 1px solid {PALETTE.border_muted};
                border-radius: 10px;
            }}
            QLabel#RemoteSessionDevice {{ color: {PALETTE.text_primary}; font-size: 11px; font-weight: 650; }}
            QLabel#RemoteSessionConnected {{ color: {PALETTE.success}; font-size: 9px; font-weight: 700; }}
            QLabel#RemoteSessionSignedIn {{ color: {PALETTE.text_muted}; font-size: 9px; font-weight: 650; }}
            QLabel#RemoteSessionMetadata {{ color: {PALETTE.text_dim}; font-size: 9px; }}
            QLabel#RemoteSessionPrompt {{ color: {PALETTE.text_secondary}; font-size: 10px; }}
            QLabel#RemoteSessionError {{ color: {PALETTE.text_muted}; font-size: 9px; }}
            QLabel#RemoteSessionError[error="true"] {{ color: {PALETTE.danger_text}; }}
            QLabel#RemoteSessionsEmpty {{ color: {PALETTE.text_muted}; font-size: 11px; padding: 18px 10px; }}
            QScrollArea#RemoteSessionsScroll {{ border: none; background: transparent; }}
            QPushButton {{
                min-height: 27px; padding: 0 9px; color: {PALETTE.text_secondary};
                background: {PALETTE.surface}; border: 1px solid {PALETTE.border};
                border-radius: 7px; font-size: 9px; font-weight: 600;
            }}
            QPushButton:hover {{ border-color: {PALETTE.accent_alt}; color: {PALETTE.text_primary}; }}
            QPushButton[destructive="true"] {{ color: {PALETTE.danger_text}; }}
            QPushButton[destructive="true"]:hover {{
                border-color: {PALETTE.danger_border}; background: {PALETTE.danger_surface};
            }}
            QPushButton:disabled {{ color: {PALETTE.text_faint}; }}
            """
        )

    def _render(self) -> None:
        self._count.setText(str(len(self._sessions)))
        if not self._enabled:
            self._summary.setText(self.tr("Disabled"))
        elif self._running:
            self._summary.setText(self.tr("Active on the local network"))
        else:
            self._summary.setText(self.tr("Unavailable"))

        self._runtime.setVisible(self._enabled and not self._running)
        self._runtime.setText(self._runtime_message or self.tr("Remote control is not running."))
        self._rebuild_rows()

        has_sessions = bool(self._sessions)
        self._scroll.setVisible(has_sessions)
        self._empty.setVisible(not has_sessions)
        if not has_sessions:
            self._empty.setText(
                self.tr("No signed-in devices. Solin is ready for a device to connect.")
                if self._running
                else self.tr("No signed-in devices.")
            )
        self._footer.setVisible(has_sessions)
        self._render_footer()
        self.adjustSize()

    def _rebuild_rows(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._rows.clear()
        for session in self._sessions:
            row = _RemoteSessionRow(session, self._list)
            row.disconnect_requested.connect(self._request_disconnect)
            row.set_pending(session.management_id in self._pending_ids)
            if session.management_id in self._failed_ids:
                row.show_error()
            self._rows[session.management_id] = row
            self._list_layout.addWidget(row)
        self._list_layout.addStretch()
        rows_height = min(self._MAX_LIST_HEIGHT, max(0, len(self._sessions) * 78))
        self._scroll.setFixedHeight(rows_height)

    def _render_footer(self) -> None:
        self._disconnect_all.setEnabled(bool(self._sessions) and not self._disconnect_all_pending)
        if self._disconnect_all_pending:
            self._disconnect_all.setText(self.tr("Disconnecting…"))
            self._disconnect_all.show()
            self._disconnect_all_confirmation.hide()
        else:
            self._disconnect_all.setText(self.tr("Disconnect all devices"))

    def _request_disconnect(self, management_id: str) -> None:
        if management_id in self._pending_ids:
            return
        self._pending_ids.add(management_id)
        self._failed_ids.discard(management_id)
        row = self._rows.get(management_id)
        if row is not None:
            row.set_pending(True)
        self.disconnect_requested.emit(management_id)

    def _show_disconnect_all_confirmation(self) -> None:
        self._disconnect_all.hide()
        self._global_feedback.hide()
        self._disconnect_all_confirmation.show()
        self.adjustSize()

    def _cancel_disconnect_all(self) -> None:
        self._disconnect_all_confirmation.hide()
        self._disconnect_all.show()
        self.adjustSize()

    def _confirm_disconnect_all(self) -> None:
        self._disconnect_all_pending = True
        self._disconnect_all_confirmation.hide()
        self._disconnect_all.show()
        self._render_footer()
        self.disconnect_all_requested.emit()

    def _refresh_activity_labels(self) -> None:
        for row in self._rows.values():
            row.refresh_activity()

    def showEvent(self, event: QEvent) -> None:
        self._activity_timer.start()
        self._refresh_activity_labels()
        super().showEvent(event)

    def hideEvent(self, event: QEvent) -> None:
        self._activity_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._title.setText(self.tr("Remote control"))
            self._disconnect_all_prompt.setText(
                self.tr(
                    "Disconnect every device? They will need to sign in again. "
                    "Projection will continue."
                )
            )
            self._disconnect_all_cancel.setText(self.tr("Cancel"))
            self._disconnect_all_confirm.setText(self.tr("Disconnect all"))
            self.apply_theme()
            self._render()
        super().changeEvent(event)
