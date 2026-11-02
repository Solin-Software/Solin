"""In-session host for the shared profile selector."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.core.profiles.models import ProfileInfo
from solin.ui.helpers import fade_in
from solin.ui.profile_selector import ProfileSelector


class ProfileSwitchOverlay(QWidget):
    """Cover the current window while preserving its session until an action."""

    cancelled = Signal()
    profile_selected = Signal(str)
    create_profile_requested = Signal()
    logout_requested = Signal()

    def __init__(
        self,
        parent_window: QWidget,
        current_profile_id: str,
        *,
        profiles: Sequence[ProfileInfo],
    ) -> None:
        super().__init__(parent_window)
        self._previous_focus = parent_window.focusWidget()
        if self._previous_focus is not None:
            self._previous_focus.destroyed.connect(self._forget_previous_focus)
        self.setGeometry(parent_window.rect())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        parent_window.installEventFilter(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._selector = ProfileSelector(
            self,
            current_profile_id=current_profile_id,
            allow_cancel=True,
            allow_logout=True,
        )
        self._selector.set_profiles(profiles)
        layout.addWidget(self._selector)
        self._selector.cancelled.connect(self._cancel)
        self._selector.profile_selected.connect(self.profile_selected)
        self._selector.create_profile_requested.connect(self.create_profile_requested)
        self._selector.logout_requested.connect(self.logout_requested)
        fade_in(self, duration=180)

    def _forget_previous_focus(self) -> None:
        self._previous_focus = None

    def _cancel(self) -> None:
        if self._previous_focus is not None and self._previous_focus.isVisible():
            self._previous_focus.setFocus(Qt.FocusReason.OtherFocusReason)
        self.cancelled.emit()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._selector.focus_initial_card()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)
