"""Collapsible main sidebar chrome."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    Property,
    QPropertyAnimation,
    Signal,
)
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from .profile_avatar_button import ProfileAvatarButton
from .sidebar_button import SidebarButton

SIDEBAR_EXPANDED_WIDTH = 220
SIDEBAR_COLLAPSED_WIDTH = 64
SIDEBAR_ANIMATION_MS = 180
SIDEBAR_EXPANSION_MIN_WIDTH = 560


class CollapsibleSidebarFrame(QFrame):
    """QFrame with a width property that can be animated cleanly."""

    def _sidebar_width(self) -> int:
        return self.width()

    def _set_sidebar_width(self, width: int) -> None:
        self.setFixedWidth(int(width))

    sidebarWidth = Property(int, _sidebar_width, _set_sidebar_width)


class SidebarChromeController(QObject):
    """Applies user preference within window-width constraints without rebuilding pages."""

    collapsed_changed = Signal(bool)

    def __init__(
        self,
        *,
        frame: CollapsibleSidebarFrame,
        width_source: QWidget,
        title_label: QLabel,
        subtitle_label: QLabel,
        profile_avatar_button: ProfileAvatarButton,
        nav_buttons: Sequence[SidebarButton],
        toggle_button: QPushButton,
        settings: Any,
        collapse_tooltip: str,
        expand_tooltip: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._frame = frame
        self._width_source = width_source
        self._title_label = title_label
        self._subtitle_label = subtitle_label
        self._profile_avatar_button = profile_avatar_button
        self._nav_buttons = tuple(nav_buttons)
        self._toggle_button = toggle_button
        self._settings = settings
        self._collapse_tooltip = collapse_tooltip
        self._expand_tooltip = expand_tooltip
        self._preferred_collapsed = bool(settings.sidebar_collapsed(False))
        self._expansion_allowed = width_source.width() >= SIDEBAR_EXPANSION_MIN_WIDTH
        self._collapsed = self._preferred_collapsed or not self._expansion_allowed

        self._animation = QPropertyAnimation(self._frame, b"sidebarWidth", self)
        self._animation.setDuration(SIDEBAR_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)

        self._toggle_button.clicked.connect(self.toggle)
        self._toggle_button.setEnabled(self._expansion_allowed)
        self._apply_width(self._target_width(self._collapsed))
        self._apply_compact(self._collapsed)
        self._width_source.installEventFilter(self)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle(self) -> None:
        if self._expansion_allowed:
            self.set_collapsed(not self._preferred_collapsed, animate=True, persist=True)

    def set_collapsed(
        self,
        collapsed: bool,
        *,
        animate: bool,
        persist: bool = False,
    ) -> None:
        collapsed = bool(collapsed)
        preference_changed = collapsed != self._preferred_collapsed
        self._preferred_collapsed = collapsed
        if persist and preference_changed:
            self._settings.save_sidebar_collapsed(collapsed)
        self._set_effective_collapsed(
            collapsed or not self._expansion_allowed,
            animate=animate,
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._width_source and event.type() == QEvent.Type.Resize:
            expansion_allowed = self._width_source.width() >= SIDEBAR_EXPANSION_MIN_WIDTH
            if expansion_allowed != self._expansion_allowed:
                self._expansion_allowed = expansion_allowed
                self._toggle_button.setEnabled(expansion_allowed)
                self._set_effective_collapsed(
                    self._preferred_collapsed or not expansion_allowed,
                    animate=False,
                )
        return super().eventFilter(watched, event)

    def _set_effective_collapsed(self, collapsed: bool, *, animate: bool) -> None:
        if (
            collapsed == self._collapsed
            and self._animation.state() == QAbstractAnimation.State.Stopped
        ):
            return

        state_changed = collapsed != self._collapsed
        if self._animation.state() != QAbstractAnimation.State.Stopped:
            self._animation.stop()

        self._collapsed = collapsed
        self._toggle_button.setToolTip(self._current_toggle_tooltip())
        if state_changed:
            self.collapsed_changed.emit(collapsed)

        self._apply_compact(collapsed)

        end_width = self._target_width(collapsed)
        if not animate:
            self._apply_width(end_width)
            self._on_animation_finished()
            return

        self._animation.setStartValue(self._frame.width())
        self._animation.setEndValue(end_width)
        self._animation.start()

    def set_toggle_tooltips(self, *, collapse: str, expand: str) -> None:
        self._collapse_tooltip = collapse
        self._expand_tooltip = expand
        self._toggle_button.setToolTip(self._current_toggle_tooltip())

    def _on_animation_finished(self) -> None:
        self._apply_width(self._target_width(self._collapsed))
        self._apply_compact(self._collapsed)

    def _apply_width(self, width: int) -> None:
        self._frame.sidebarWidth = width

    def _apply_compact(self, compact: bool) -> None:
        self._set_text_visible(not compact)
        self._set_nav_compact(compact)
        self._toggle_button.setToolTip(self._current_toggle_tooltip())

    def _set_text_visible(self, visible: bool) -> None:
        self._title_label.setVisible(visible)
        self._subtitle_label.setVisible(visible)
        self._profile_avatar_button.setVisible(visible)

    def _set_nav_compact(self, compact: bool) -> None:
        for button in self._nav_buttons:
            button.set_compact(compact)

    def _current_toggle_tooltip(self) -> str:
        return self._expand_tooltip if self._collapsed else self._collapse_tooltip

    @staticmethod
    def _target_width(collapsed: bool) -> int:
        return SIDEBAR_COLLAPSED_WIDTH if collapsed else SIDEBAR_EXPANDED_WIDTH
