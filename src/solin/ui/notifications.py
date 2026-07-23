"""Centralized transient notifications for the main application window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any

from PySide6.QtCore import QMargins, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QWidget

from solin.styles.theme import PALETTE


class NotificationKind(Enum):
    SUCCESS = "success"
    INFORMATION = "information"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class _NotificationStyle:
    preset: str
    accent: str
    duration_ms: int


_STYLES = {
    NotificationKind.SUCCESS: _NotificationStyle(
        "SUCCESS_DARK",
        PALETTE.success,
        3200,
    ),
    NotificationKind.INFORMATION: _NotificationStyle(
        "INFORMATION_DARK",
        PALETTE.accent,
        3800,
    ),
    NotificationKind.WARNING: _NotificationStyle(
        "WARNING_DARK",
        PALETTE.warning,
        5200,
    ),
    NotificationKind.ERROR: _NotificationStyle(
        "ERROR_DARK",
        PALETTE.danger,
        7000,
    ),
}

NOTIFICATION_ICONS = {
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


class NotificationCenter:
    """Single entry point for transient, non-modal application feedback."""

    def __init__(
        self,
        anchor: QWidget,
        *,
        toast_type: type[Any] | None = None,
        clock: Callable[[], float] = monotonic,
        dedupe_window_seconds: float = 3.0,
    ) -> None:
        self._anchor = anchor
        self._toast_type = toast_type
        self._positioning_configured = False
        self._clock = clock
        self._dedupe_window_seconds = dedupe_window_seconds
        self._last_shown_at: dict[str, float] = {}
        if self._toast_type is not None:
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
        toast_type, toast_preset = self._ensure_backend(style.preset)
        toast = toast_type(self._anchor)
        toast.applyPreset(toast_preset)
        toast.setTitle(title or message)
        toast.setText(message if title else "")
        toast.setDuration(duration_ms if duration_ms is not None else style.duration_ms)
        self._apply_theme(toast, style.accent)
        if hasattr(toast, "set_notification_icon"):
            toast.set_notification_icon(kind, style.accent)
        toast.show()
        return True

    def shutdown(self) -> None:
        if (
            self._toast_type is not None
            and self._toast_type.getPositionRelativeToWidget() is self._anchor
        ):
            self._toast_type.reset()
        self._last_shown_at.clear()

    def _configure_positioning(self) -> None:
        if self._positioning_configured or self._toast_type is None:
            return
        from pyqttoast import ToastPosition

        self._toast_type.setPositionRelativeToWidget(self._anchor)
        self._toast_type.setMovePositionWithWidget(True)
        self._toast_type.setPosition(ToastPosition.BOTTOM_RIGHT)
        self._toast_type.setOffset(20, 64)
        self._toast_type.setSpacing(10)
        self._toast_type.setMaximumOnScreen(3)
        self._positioning_configured = True

    def _ensure_backend(self, preset_name: str) -> tuple[type[Any], Any]:
        from pyqttoast import ToastPreset

        if self._toast_type is None:
            from solin.ui.toast_backend import SynchronizedToast

            self._toast_type = SynchronizedToast
        self._configure_positioning()
        return self._toast_type, getattr(ToastPreset, preset_name)

    def _is_duplicate(self, key: str) -> bool:
        now = self._clock()
        last_shown = self._last_shown_at.get(key)
        if last_shown is not None and now - last_shown < self._dedupe_window_seconds:
            return True
        self._last_shown_at[key] = now
        return False

    @staticmethod
    def _apply_theme(toast: Any, accent: str) -> None:
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

        toast.setBackgroundColor(QColor(PALETTE.surface))
        toast.setTitleColor(QColor(PALETTE.text_primary))
        toast.setTextColor(QColor(PALETTE.text_muted))
        toast.setCloseButtonIconColor(QColor(PALETTE.text_dim))
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


__all__ = [
    "NotificationCenter",
    "NotificationKind",
]
