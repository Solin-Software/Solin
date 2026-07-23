from __future__ import annotations

import math
from time import monotonic

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QWidget
from pyqttoast import Toast

from solin.styles.icons import make_icon
from solin.ui.notifications import NOTIFICATION_ICONS, NotificationKind


class SynchronizedToast(Toast):
    """Keep the duration bar aligned with the actual close timer."""

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
        self.setIcon(make_icon(NOTIFICATION_ICONS[kind], size.width(), color).pixmap(size))
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


__all__ = ["SynchronizedToast"]
