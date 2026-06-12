"""Centralized transient notifications for the main application window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import math
from time import monotonic

from PySide6.QtCore import QMargins, QRect, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QWidget
from pyqttoast import Toast, ToastPosition, ToastPreset

from app.styles.icons import make_icon
from app.styles.theme import COLORS


class NotificationKind(Enum):
    SUCCESS = "success"
    INFORMATION = "information"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class _NotificationStyle:
    preset: ToastPreset
    accent: str
    duration_ms: int


_STYLES = {
    NotificationKind.SUCCESS: _NotificationStyle(
        ToastPreset.SUCCESS_DARK,
        COLORS["success"],
        3200,
    ),
    NotificationKind.INFORMATION: _NotificationStyle(
        ToastPreset.INFORMATION_DARK,
        COLORS["accent2"],
        3800,
    ),
    NotificationKind.WARNING: _NotificationStyle(
        ToastPreset.WARNING_DARK,
        COLORS["warning"],
        5200,
    ),
    NotificationKind.ERROR: _NotificationStyle(
        ToastPreset.ERROR_DARK,
        COLORS["danger"],
        7000,
    ),
}

_ICONS = {
    NotificationKind.SUCCESS: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
        ' stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
        '<path d="m8 12 2.6 2.6L16.5 9"/></svg>'
    ),
    NotificationKind.INFORMATION: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
        ' stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
        '<path d="M12 11v5"/><path d="M12 8h.01"/></svg>'
    ),
    NotificationKind.WARNING: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
        ' stroke-linejoin="round"><path d="M10.3 4.2 2.4 18a2 2 0 0 0 1.7 3h15.8'
        'a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"/>'
        '<path d="M12 9v4"/><path d="M12 17h.01"/></svg>'
    ),
    NotificationKind.ERROR: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
        ' stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>'
        '<path d="m9 9 6 6M15 9l-6 6"/></svg>'
    ),
}


class _SynchronizedToast(Toast):
    """Keep the duration bar aligned with the actual close timer.

    pyqt-toast-notification 1.3.3 advances its bar by counting 1 ms timer
    callbacks. Qt may coalesce those callbacks under load, while the close
    timer still expires on wall-clock time, leaving a partially filled bar.
    """

    _BAR_UPDATE_INTERVAL_MS = 16
    _BAR_HEIGHT = 4
    _BAR_HORIZONTAL_INSET = 10
    _BAR_BOTTOM_INSET = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration_started_at: float | None = None
        bar_timer = self._Toast__duration_bar_timer
        bar_timer.timeout.disconnect(self._Toast__update_duration_bar)
        bar_timer.timeout.connect(self._sync_duration_bar)

    def show(self) -> None:
        self._duration_started_at = None
        super().show()
        if not self.isVisible():
            return
        self._duration_started_at = monotonic()
        self._configure_duration_bar()
        self._restart_bar_timer()

    def hide(self) -> None:
        self._set_bar_width(0)
        super().hide()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        if self.isResetDurationOnHover():
            self._duration_started_at = None

    def leaveEvent(self, event) -> None:
        if self.isResetDurationOnHover():
            self._duration_started_at = monotonic()
        super().leaveEvent(event)
        self._restart_bar_timer()

    def _restart_bar_timer(self) -> None:
        bar_timer = self._Toast__duration_bar_timer
        if bar_timer.isActive():
            bar_timer.start(self._BAR_UPDATE_INTERVAL_MS)

    def _sync_duration_bar(self) -> None:
        duration_ms = self.getDuration()
        if duration_ms <= 0 or self._duration_started_at is None:
            return
        elapsed_ms = (monotonic() - self._duration_started_at) * 1000
        container_width = self._Toast__duration_bar_container.width()
        self._set_bar_width(
            self._remaining_bar_width(container_width, duration_ms, elapsed_ms)
        )
        if elapsed_ms >= duration_ms:
            self._Toast__duration_bar_timer.stop()

    def _set_bar_width(self, width: int) -> None:
        self._Toast__duration_bar_chunk.setFixedWidth(max(0, width))

    def _configure_duration_bar(self) -> None:
        toast_widget = self._Toast__toast_widget
        geometry = self._duration_bar_geometry(
            toast_widget.width(),
            toast_widget.height(),
        )
        container = self._Toast__duration_bar_container
        track = self._Toast__duration_bar
        chunk = self._Toast__duration_bar_chunk
        color = self.getDurationBarColor()
        radius = self._BAR_HEIGHT // 2

        container.setFixedSize(geometry.size())
        container.move(geometry.topLeft())
        track.setFixedSize(geometry.size())
        track.move(0, 0)
        chunk.setFixedSize(geometry.size())
        chunk.move(0, 0)
        container.setStyleSheet("background: transparent;")
        track.setStyleSheet(
            f"background: rgba({color.red()}, {color.green()}, {color.blue()}, 70);"
            f"border-radius: {radius}px;"
        )
        chunk.setStyleSheet(
            f"background: rgba({color.red()}, {color.green()}, {color.blue()}, 255);"
            f"border-radius: {radius}px;"
        )

    def set_notification_icon(self, kind: NotificationKind, color: str) -> None:
        size = QSize(20, 20)
        self.setIconColor(None)
        self.setIcon(make_icon(_ICONS[kind], size.width(), color).pixmap(size))
        self.setIconSize(size)
        self._Toast__icon_widget.setStyleSheet(
            "background: transparent; border: none; padding: 0;"
        )

    @classmethod
    def _duration_bar_geometry(cls, toast_width: int, toast_height: int) -> QRect:
        width = max(0, toast_width - cls._BAR_HORIZONTAL_INSET * 2)
        y = max(0, toast_height - cls._BAR_BOTTOM_INSET - cls._BAR_HEIGHT)
        return QRect(
            cls._BAR_HORIZONTAL_INSET,
            y,
            width,
            cls._BAR_HEIGHT,
        )

    @staticmethod
    def _remaining_bar_width(
        total_width: int,
        duration_ms: int,
        elapsed_ms: float,
    ) -> int:
        if total_width <= 0 or duration_ms <= 0 or elapsed_ms >= duration_ms:
            return 0
        remaining_ratio = max(0.0, 1.0 - elapsed_ms / duration_ms)
        return math.ceil(total_width * remaining_ratio)


class NotificationCenter:
    """Single entry point for transient, non-modal application feedback."""

    def __init__(
        self,
        anchor: QWidget,
        *,
        toast_type: type[Toast] = _SynchronizedToast,
        clock: Callable[[], float] = monotonic,
        dedupe_window_seconds: float = 3.0,
    ) -> None:
        self._anchor = anchor
        self._toast_type = toast_type
        self._clock = clock
        self._dedupe_window_seconds = dedupe_window_seconds
        self._last_shown_at: dict[str, float] = {}
        self._configure_positioning()

    def success(
        self,
        message: str,
        *,
        title: str = "",
        duration_ms: int | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify(
            NotificationKind.SUCCESS,
            message,
            title=title,
            duration_ms=duration_ms,
            dedupe_key=dedupe_key,
        )

    def information(
        self,
        message: str,
        *,
        title: str = "",
        duration_ms: int | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify(
            NotificationKind.INFORMATION,
            message,
            title=title,
            duration_ms=duration_ms,
            dedupe_key=dedupe_key,
        )

    def warning(
        self,
        message: str,
        *,
        title: str = "",
        duration_ms: int | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify(
            NotificationKind.WARNING,
            message,
            title=title,
            duration_ms=duration_ms,
            dedupe_key=dedupe_key,
        )

    def error(
        self,
        message: str,
        *,
        title: str = "",
        duration_ms: int | None = None,
        dedupe_key: str = "",
    ) -> bool:
        return self.notify(
            NotificationKind.ERROR,
            message,
            title=title,
            duration_ms=duration_ms,
            dedupe_key=dedupe_key,
        )

    def notify(
        self,
        kind: NotificationKind,
        message: str,
        *,
        title: str = "",
        duration_ms: int | None = None,
        dedupe_key: str = "",
    ) -> bool:
        message = (message or "").strip()
        title = (title or "").strip()
        if not message:
            return False
        if dedupe_key and self._is_duplicate(dedupe_key):
            return False

        style = _STYLES[kind]
        toast = self._toast_type(self._anchor)
        toast.applyPreset(style.preset)
        toast.setTitle(title or message)
        toast.setText(message if title else "")
        toast.setDuration(duration_ms if duration_ms is not None else style.duration_ms)
        self._apply_theme(toast, style.accent)
        if isinstance(toast, _SynchronizedToast):
            toast.set_notification_icon(kind, style.accent)
        toast.show()
        return True

    def shutdown(self) -> None:
        if self._toast_type.getPositionRelativeToWidget() is self._anchor:
            self._toast_type.reset()
        self._last_shown_at.clear()

    def _configure_positioning(self) -> None:
        self._toast_type.setPositionRelativeToWidget(self._anchor)
        self._toast_type.setMovePositionWithWidget(True)
        self._toast_type.setPosition(ToastPosition.BOTTOM_RIGHT)
        self._toast_type.setOffset(20, 64)
        self._toast_type.setSpacing(10)
        self._toast_type.setMaximumOnScreen(3)

    def _is_duplicate(self, key: str) -> bool:
        now = self._clock()
        last_shown = self._last_shown_at.get(key)
        if last_shown is not None and now - last_shown < self._dedupe_window_seconds:
            return True
        self._last_shown_at[key] = now
        return False

    @staticmethod
    def _apply_theme(toast: Toast, accent: str) -> None:
        accent_color = QColor(accent)
        toast.setFixedWidth(360)
        toast.setStayOnTop(False)
        toast.setResetDurationOnHover(True)
        toast.setShowCloseButton(True)
        toast.setShowDurationBar(True)
        toast.setShowIcon(True)
        toast.setShowIconSeparator(False)
        toast.setBorderRadius(10)
        toast.setFadeInDuration(180)
        toast.setFadeOutDuration(220)

        toast.setBackgroundColor(QColor(COLORS["bg1"]))
        toast.setTitleColor(QColor(COLORS["text_primary"]))
        toast.setTextColor(QColor(COLORS["text_secondary"]))
        toast.setCloseButtonIconColor(QColor(COLORS["text_muted"]))
        toast.setIconColor(accent_color)
        toast.setDurationBarColor(accent_color)

        toast.setTitleFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        toast.setTextFont(QFont("Segoe UI", 9))
        toast.setIconSize(QSize(20, 20))
        toast.setCloseButtonIconSize(QSize(10, 10))
        toast.setCloseButtonSize(QSize(24, 24))
        toast.setMargins(QMargins(16, 14, 10, 14))
        toast.setIconSectionMargins(QMargins(0, 0, 12, 0))
        toast.setTextSectionMargins(QMargins(0, 0, 10, 0))
        toast.setCloseButtonMargins(QMargins(0, -4, 0, -4))
        toast.setTextSectionSpacing(5)
