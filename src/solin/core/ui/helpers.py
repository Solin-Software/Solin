"""Shared UI helpers used across Solin widgets."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


AVATAR_COLORS: tuple[tuple[str, str], ...] = (
    ("#1f6feb", "#388bfd"),
    ("#238636", "#3fb950"),
    ("#8957e5", "#a371f7"),
    ("#bf8700", "#d29922"),
    ("#cf222e", "#f85149"),
    ("#1a7f37", "#56d364"),
    ("#0550ae", "#4493f8"),
    ("#6e40c9", "#d2a8ff"),
)


def avatar_colors(profile_id: str) -> tuple[str, str]:
    """Return the deterministic gradient colors for a profile/avatar id."""
    idx = sum(ord(c) for c in profile_id) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def initials(name: str) -> str:
    """Return one or two uppercase initials for a display name."""
    parts = name.strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def fade_in(widget: QWidget, duration: int = 300) -> None:
    """Fade in a widget with a short cubic opacity animation."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def make_rounded_thumb(
    pixmap: QPixmap,
    w: int = 68,
    h: int = 40,
    radius: int = 4,
) -> QPixmap:
    """Scale, center-crop and round a thumbnail pixmap."""
    scaled = pixmap.scaled(
        w,
        h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.width() > w or scaled.height() > h:
        x = (scaled.width() - w) // 2
        y = (scaled.height() - h) // 2
        scaled = scaled.copy(x, y, w, h)

    rounded = QPixmap(w, h)
    rounded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return rounded


def begin_qml_pointer_cursor(qml_widget: QWidget) -> None:
    """Set the hand cursor on a QQuickWidget host and its native quick window."""
    qml_widget.setCursor(Qt.CursorShape.PointingHandCursor)
    for window in _qml_cursor_windows(qml_widget):
        window.setCursor(Qt.CursorShape.PointingHandCursor)


def end_qml_pointer_cursor(qml_widget: QWidget) -> None:
    """Clear the hand cursor applied by begin_qml_pointer_cursor."""
    qml_widget.unsetCursor()
    for window in _qml_cursor_windows(qml_widget):
        window.unsetCursor()


def _qml_cursor_windows(qml_widget: QWidget) -> list:
    windows = []
    quick_window = qml_widget.quickWindow() if hasattr(qml_widget, "quickWindow") else None
    if quick_window is not None:
        windows.append(quick_window)

    host_widget = qml_widget.window() if hasattr(qml_widget, "window") else None
    host_window = (
        host_widget.windowHandle()
        if host_widget is not None and hasattr(host_widget, "windowHandle")
        else None
    )
    if host_window is not None and not any(host_window is window for window in windows):
        windows.append(host_window)
    return windows
