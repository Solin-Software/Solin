from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QHideEvent, QShowEvent
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

from solin.core.remote_control.security import RemoteScope, RemoteSessionInfo
from solin.styles.icons import ICON_REMOTE_CONTROL, make_icon
from solin.styles.theme import PALETTE, qss_rgba


class _RemoteSessionRow(QFrame):
    disconnect_requested = Signal(str)
    geometry_changed = Signal()

    def __init__(self, session: RemoteSessionInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("RemoteSessionRow")
        self.setMinimumHeight(88)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 12, 12)
        root.setSpacing(8)

        summary = QHBoxLayout()
        summary.setSpacing(8)
        device = QLabel(self._session_label())
        device.setObjectName("RemoteSessionDevice")
        device.setMinimumWidth(0)
        summary.addWidget(device, 1)

        scope_badge = QLabel(self._scope_label())
        scope_badge.setObjectName(
            "RemoteSessionScopeTimer"
            if session.scope is RemoteScope.TIMER
            else "RemoteSessionScopeMedia"
        )
        summary.addWidget(scope_badge)

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

    def _scope_label(self) -> str:
        if self.session.scope is RemoteScope.TIMER:
            return self.tr("Timer")
        return self.tr("Media")

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
        self._notify_geometry_changed()

    def show_error(self) -> None:
        self._disconnect.setEnabled(True)
        self._feedback.setProperty("error", True)
        self._feedback.setText(self.tr("Could not disconnect this device. Try again."))
        self._feedback.show()
        self.style().unpolish(self._feedback)
        self.style().polish(self._feedback)
        self._notify_geometry_changed()

    def _show_confirmation(self) -> None:
        self._disconnect.hide()
        self._feedback.hide()
        self._confirmation.show()
        self._notify_geometry_changed()

    def _cancel_confirmation(self) -> None:
        self._confirmation.hide()
        self._disconnect.show()
        self._notify_geometry_changed()

    def _confirm_disconnect(self) -> None:
        self._disconnect.show()
        self.set_pending(True)
        self.disconnect_requested.emit(self.session.global_management_id)

    def _notify_geometry_changed(self) -> None:
        self.updateGeometry()
        self.geometry_changed.emit()

    def _session_label(self) -> str:
        browser = self.session.browser or self.tr("Remote device")
        platform = self.session.platform
        if browser and platform:
            label = self.tr("%1 on %2").replace("%1", browser).replace("%2", platform)
        else:
            label = browser or platform or self.tr("Remote device")
        if self.session.client_mode == "standalone":
            if self.session.scope is RemoteScope.TIMER:
                return self.tr("Solin Timer · %1").replace("%1", label)
            return self.tr("Solin Media · %1").replace("%1", label)
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

    _WIDTH = 404
    _MAX_LIST_HEIGHT = 368
    _SCREEN_MARGIN = 8
    _ANCHOR_GAP = 10

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._WIDTH)

        self._enabled = False
        self._running = False
        self._media_active = False
        self._timer_active = False
        self._timer_public_viewers = 0
        self._runtime_message = ""
        self._sessions: tuple[RemoteSessionInfo, ...] = ()
        self._rows: dict[str, _RemoteSessionRow] = {}
        self._section_widgets: list[QWidget] = []
        self._pending_ids: set[str] = set()
        self._failed_ids: set[str] = set()
        self._disconnect_all_pending = False
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None

        opacity = QGraphicsOpacityEffect(self)
        opacity.setOpacity(1.0)
        self.setGraphicsEffect(opacity)
        self._opacity = opacity
        self._fade = QPropertyAnimation(opacity, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._finish_show)
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
        card.setContentsMargins(18, 17, 18, 16)
        card.setSpacing(13)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._header_icon = QLabel()
        self._header_icon.setFixedSize(20, 20)
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
        self._count.setFixedSize(24, 24)
        self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._count, alignment=Qt.AlignmentFlag.AlignVCenter)
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
        footer_layout.setContentsMargins(0, 3, 0, 0)
        footer_layout.setSpacing(8)
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

    def set_runtime_state(
        self,
        enabled: bool,
        running: bool,
        message: str,
        *,
        media_active: bool = False,
        timer_active: bool = False,
    ) -> None:
        self._enabled = bool(enabled)
        self._running = bool(running)
        self._media_active = bool(media_active)
        self._timer_active = bool(timer_active)
        self._runtime_message = str(message or "")
        self._render()

    def set_public_viewers(self, timer_viewers: int = 0) -> None:
        self._timer_public_viewers = max(0, int(timer_viewers))
        self._render()

    def set_sessions(self, sessions: object) -> None:
        values = (
            tuple(session for session in sessions if isinstance(session, RemoteSessionInfo))
            if isinstance(sessions, (tuple, list))
            else ()
        )
        self._sessions = values
        active_ids = {session.global_management_id for session in values}
        self._pending_ids.intersection_update(active_ids)
        self._failed_ids.intersection_update(active_ids)
        if not values:
            self._disconnect_all_pending = False
        self._render()

    def set_revocation_result(self, management_id: str, succeeded: bool) -> None:
        if management_id:
            target_id = self._resolve_row_id(management_id)
            self._pending_ids.discard(target_id)
            self._failed_ids.discard(target_id)
            row = self._rows.get(target_id)
            if row is not None:
                if succeeded:
                    row.set_pending(True)
                else:
                    self._failed_ids.add(target_id)
                    row.show_error()
            return
        self._disconnect_all_pending = False
        if not succeeded:
            self._global_feedback.setText(self.tr("Could not disconnect all devices. Try again."))
            self._global_feedback.show()
        else:
            self._global_feedback.hide()
        self._render_footer()
        self._sync_geometry()

    def _resolve_row_id(self, identifier: str) -> str:
        if identifier in self._rows:
            return identifier
        for key, row in self._rows.items():
            if row.session.management_id == identifier:
                return key
        return identifier

    def show_above(self, anchor: QWidget) -> None:
        anchor_top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        self._anchor_rect = QRect(
            anchor_top_left.x(),
            anchor_top_left.y(),
            anchor.width(),
            anchor.height(),
        )
        anchor_center = self._anchor_rect.center()
        screen = QGuiApplication.screenAt(anchor_center) or QGuiApplication.primaryScreen()
        self._available_rect = screen.availableGeometry() if screen is not None else None

        self._fade.stop()
        self._opacity.setOpacity(0.0)
        self._render()
        self.ensurePolished()
        self._sync_geometry()
        self.show()
        self._show_timer.start(0)

    def _finish_show(self) -> None:
        if not self.isVisible():
            return
        self._sync_geometry()
        self.raise_()
        self.activateWindow()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _sync_geometry(self) -> None:
        self.setMinimumHeight(0)
        self.setMaximumHeight(16_777_215)
        card_layout = self._card.layout()
        if card_layout is not None:
            card_layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()
        self.setFixedSize(self._WIDTH, max(1, self.sizeHint().height()))

        if self._anchor_rect is None:
            return
        x, y = self._popup_position(
            self._anchor_rect,
            self._available_rect,
            self.width(),
            self.height(),
        )
        self.move(x, y)

    @classmethod
    def _popup_position(
        cls,
        anchor: QRect,
        available: QRect | None,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        x = anchor.center().x() - width // 2
        y_above = anchor.top() - height - cls._ANCHOR_GAP
        y_below = anchor.bottom() + cls._ANCHOR_GAP + 1
        y = y_above
        if available is None:
            return x, y

        left = available.left() + cls._SCREEN_MARGIN
        right = available.right() - cls._SCREEN_MARGIN
        top = available.top() + cls._SCREEN_MARGIN
        bottom = available.bottom() - cls._SCREEN_MARGIN
        x = max(left, min(x, right - width + 1))
        if y_above < top and y_below + height - 1 <= bottom:
            y = y_below
        else:
            y = max(top, min(y_above, bottom - height + 1))
        return x, y

    def apply_theme(self) -> None:
        self._header_icon.setPixmap(
            make_icon(ICON_REMOTE_CONTROL, 20, PALETTE.accent).pixmap(20, 20)
        )
        self.setStyleSheet(
            f"""
            QFrame#RemoteSessionsCard {{
                background: {PALETTE.surface};
                border: 1px solid {qss_rgba(PALETTE.border, 0.85)}; border-radius: 16px;
            }}
            QWidget {{ background: transparent; color: {PALETTE.text_secondary}; }}
            QLabel#RemoteSessionsTitle {{ color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700; }}
            QLabel#RemoteSessionsSummary {{ color: {PALETTE.text_muted}; font-size: 10px; }}
            QLabel#RemoteSessionsCount {{
                min-width: 24px; min-height: 24px; color: {PALETTE.accent_text_hover};
                background: {PALETTE.accent_muted}; border-radius: 12px;
                font-size: 10px; font-weight: 700;
            }}
            QLabel#RemoteSessionsRuntime {{
                color: {PALETTE.warning_text}; background: {PALETTE.warning_surface};
                border: 1px solid {PALETTE.warning_border}; border-radius: 8px;
                padding: 8px 9px; font-size: 10px;
            }}
            QLabel#RemoteScopeHeaderTitle {{
                color: {PALETTE.text_secondary}; font-size: 10px; font-weight: 750;
                letter-spacing: 0.6px;
            }}
            QLabel#RemoteScopeHeaderMeta {{
                color: {PALETTE.text_muted}; font-size: 10px;
            }}
            QLabel#RemoteScopeEmpty {{
                color: {PALETTE.text_dim}; font-size: 10px; padding: 6px 10px 8px 10px;
                background: {PALETTE.surface_card}; border: 1px solid {PALETTE.border_muted};
                border-radius: 9px;
            }}
            QFrame#RemoteSessionRow {{
                background: {PALETTE.surface_card}; border: 1px solid {PALETTE.border_muted};
                border-radius: 10px;
            }}
            QLabel#RemoteSessionDevice {{ color: {PALETTE.text_primary}; font-size: 12px; font-weight: 650; }}
            QLabel#RemoteSessionScopeMedia, QLabel#RemoteSessionScopeTimer {{
                color: {PALETTE.accent_text_hover}; background: {PALETTE.accent_muted};
                border-radius: 6px; padding: 2px 6px; font-size: 9px; font-weight: 700;
            }}
            QLabel#RemoteSessionConnected {{ color: {PALETTE.success}; font-size: 10px; font-weight: 700; }}
            QLabel#RemoteSessionSignedIn {{ color: {PALETTE.text_muted}; font-size: 10px; font-weight: 650; }}
            QLabel#RemoteSessionMetadata {{ color: {PALETTE.text_dim}; font-size: 10px; }}
            QLabel#RemoteSessionPrompt {{ color: {PALETTE.text_secondary}; font-size: 10px; }}
            QLabel#RemoteSessionError {{ color: {PALETTE.text_muted}; font-size: 9px; }}
            QLabel#RemoteSessionError[error="true"] {{ color: {PALETTE.danger_text}; }}
            QLabel#RemoteSessionsEmpty {{ color: {PALETTE.text_muted}; font-size: 11px; padding: 18px 10px; }}
            QScrollArea#RemoteSessionsScroll {{ border: none; background: transparent; }}
            QScrollBar:vertical {{
                width: 3px; background: transparent; margin: 4px 0 4px 2px;
            }}
            QScrollBar::handle:vertical {{
                min-height: 20px; border-radius: 1px;
                background: {qss_rgba(PALETTE.text_faint, 0.30)};
            }}
            QScrollBar::handle:vertical:hover {{
                background: {qss_rgba(PALETTE.text_faint, 0.50)};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QPushButton {{
                min-height: 30px; padding: 0 10px; color: {PALETTE.text_secondary};
                background: {PALETTE.surface}; border: 1px solid {PALETTE.border};
                border-radius: 8px; font-size: 10px; font-weight: 600;
            }}
            QPushButton:hover {{ border-color: {PALETTE.accent_alt}; color: {PALETTE.text_primary}; }}
            QPushButton[destructive="true"] {{ color: {PALETTE.danger_text}; }}
            QPushButton[destructive="true"]:hover {{
                border-color: {PALETTE.danger_border}; background: {PALETTE.danger_surface};
            }}
            QPushButton:disabled {{ color: {PALETTE.text_faint}; }}
            """
        )

    def _summary_text(self) -> str:
        if not self._enabled:
            return self.tr("Disabled")
        if not self._running:
            return self.tr("Unavailable")
        if self._media_active and self._timer_active:
            return self.tr("Media and Timer active on local network")
        if self._media_active:
            return self.tr("Media active on local network")
        if self._timer_active:
            return self.tr("Timer active on local network")
        return self.tr("Active on the local network")

    def _visible_scopes(self) -> tuple[RemoteScope, ...]:
        media_sessions = any(s.scope is RemoteScope.MEDIA for s in self._sessions)
        timer_sessions = any(s.scope is RemoteScope.TIMER for s in self._sessions)
        scopes: list[RemoteScope] = []
        if self._media_active or media_sessions:
            scopes.append(RemoteScope.MEDIA)
        if self._timer_active or timer_sessions or self._timer_public_viewers > 0:
            scopes.append(RemoteScope.TIMER)
        return tuple(scopes)

    def _render(self) -> None:
        self._count.setText(str(len(self._sessions)))
        self._summary.setText(self._summary_text())

        self._runtime.setVisible(self._enabled and not self._running)
        self._runtime.setText(self._runtime_message or self.tr("Remote control is not running."))
        self._rebuild_rows()

        has_sessions = bool(self._sessions)
        has_content = has_sessions or self._timer_public_viewers > 0 or len(self._visible_scopes()) > 1
        show_list = bool(self._section_widgets or self._rows) and (
            has_sessions or self._timer_public_viewers > 0
        )
        self._scroll.setVisible(show_list)
        self._empty.setVisible(not show_list)
        if not show_list:
            self._empty.setText(
                self.tr("No signed-in devices. Solin is ready for a device to connect.")
                if self._running
                else self.tr("No signed-in devices.")
            )
        del has_content
        self._footer.setVisible(has_sessions)
        self._render_footer()
        self._sync_geometry()

    def _rebuild_rows(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item is not None and (widget := item.widget()):
                widget.deleteLater()
        self._rows.clear()
        self._section_widgets.clear()

        scopes = self._visible_scopes()
        if not scopes and self._sessions:
            scopes = tuple(dict.fromkeys(session.scope for session in self._sessions))

        has_any_list_content = bool(self._sessions) or self._timer_public_viewers > 0
        if has_any_list_content:
            for scope in scopes:
                scope_sessions = [s for s in self._sessions if s.scope is scope]
                header = self._build_scope_header(scope, len(scope_sessions))
                self._section_widgets.append(header)
                self._list_layout.addWidget(header)

                if scope_sessions:
                    for session in scope_sessions:
                        row = _RemoteSessionRow(session, self._list)
                        row.disconnect_requested.connect(self._request_disconnect)
                        row.geometry_changed.connect(self._reflow_rows)
                        row_id = session.global_management_id
                        row.set_pending(row_id in self._pending_ids)
                        if row_id in self._failed_ids:
                            row.show_error()
                        self._rows[row_id] = row
                        self._list_layout.addWidget(row)
                else:
                    empty = QLabel(
                        self.tr("No signed-in Timer controllers")
                        if scope is RemoteScope.TIMER
                        else self.tr("No signed-in Media devices")
                    )
                    empty.setObjectName("RemoteScopeEmpty")
                    empty.setWordWrap(True)
                    self._section_widgets.append(empty)
                    self._list_layout.addWidget(empty)

        self._list_layout.addStretch()
        self._reflow_rows()

    def _build_scope_header(self, scope: RemoteScope, session_count: int) -> QWidget:
        container = QWidget(self._list)
        container.setObjectName(f"RemoteScopeHeader_{scope.value}")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(8)
        title = QLabel(
            self.tr("Timer") if scope is RemoteScope.TIMER else self.tr("Media")
        )
        title.setObjectName("RemoteScopeHeaderTitle")
        layout.addWidget(title)
        layout.addStretch()

        meta_parts: list[str] = []
        if scope is RemoteScope.TIMER and self._timer_public_viewers > 0:
            meta_parts.append(
                self.tr(
                    "%n fullscreen viewer(s)",
                    "",
                    self._timer_public_viewers,
                )
            )
        meta_parts.append(
            self.tr("%n signed-in device(s)", "", session_count)
        )
        meta = QLabel(" · ".join(meta_parts))
        meta.setObjectName("RemoteScopeHeaderMeta")
        layout.addWidget(meta)
        return container

    def _reflow_rows(self) -> None:
        self._list_layout.activate()
        section_heights = sum(
            max(22, widget.sizeHint().height()) for widget in self._section_widgets
        )
        row_heights = sum(max(88, row.sizeHint().height()) for row in self._rows.values())
        item_count = len(self._section_widgets) + len(self._rows)
        row_spacing = max(0, item_count - 1) * self._list_layout.spacing()
        rows_height = min(
            self._MAX_LIST_HEIGHT,
            section_heights + row_heights + row_spacing,
        )
        self._scroll.setFixedHeight(rows_height)
        self._sync_geometry()

    def _render_footer(self) -> None:
        self._disconnect_all.setEnabled(bool(self._sessions) and not self._disconnect_all_pending)
        if self._disconnect_all_pending:
            self._disconnect_all.setText(self.tr("Disconnecting…"))
            self._disconnect_all.show()
            self._disconnect_all_confirmation.hide()
        else:
            self._disconnect_all.setText(self.tr("Disconnect all devices"))

    def _request_disconnect(self, management_id: str) -> None:
        target_id = self._resolve_row_id(management_id)
        if target_id in self._pending_ids:
            return
        self._pending_ids.add(target_id)
        self._failed_ids.discard(target_id)
        row = self._rows.get(target_id)
        if row is not None:
            row.set_pending(True)
        self.disconnect_requested.emit(target_id)

    def _show_disconnect_all_confirmation(self) -> None:
        self._disconnect_all.hide()
        self._global_feedback.hide()
        self._disconnect_all_confirmation.show()
        self._sync_geometry()

    def _cancel_disconnect_all(self) -> None:
        self._disconnect_all_confirmation.hide()
        self._disconnect_all.show()
        self._sync_geometry()

    def _confirm_disconnect_all(self) -> None:
        self._disconnect_all_pending = True
        self._disconnect_all_confirmation.hide()
        self._disconnect_all.show()
        self._render_footer()
        self._sync_geometry()
        self.disconnect_all_requested.emit()

    def _refresh_activity_labels(self) -> None:
        for row in self._rows.values():
            row.refresh_activity()

    def showEvent(self, event: QShowEvent) -> None:
        self._activity_timer.start()
        self._refresh_activity_labels()
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._show_timer.stop()
        self._fade.stop()
        self._opacity.setOpacity(1.0)
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
