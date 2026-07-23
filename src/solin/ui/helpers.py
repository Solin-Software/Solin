"""Shared presentation helpers used across Solin widgets."""

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


def avatar_colors(avatar_key: str) -> tuple[str, str]:
    """Return deterministic gradient colors for a stable avatar key."""
    idx = sum(ord(c) for c in avatar_key) % len(AVATAR_COLORS)
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
    effect.setObjectName("_solin_fade_in_effect")
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def cleanup_effect() -> None:
        current = widget.graphicsEffect()
        if current is effect or (
            current is not None
            and current.objectName() == "_solin_fade_in_effect"
        ):
            widget.setGraphicsEffect(None)

    anim.finished.connect(cleanup_effect)
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


def set_qml_pointer_cursor(
    qml_widget: QWidget,
    cursor_shape: Qt.CursorShape | int,
) -> None:
    """Apply one cursor shape to a QQuickWidget and all native cursor owners."""
    shape = Qt.CursorShape(cursor_shape)
    qml_widget.setCursor(shape)
    for window in _qml_cursor_windows(qml_widget):
        window.setCursor(shape)


def begin_qml_pointer_cursor(qml_widget: QWidget) -> None:
    """Set the pointing-hand cursor on a QQuickWidget host."""
    set_qml_pointer_cursor(qml_widget, Qt.CursorShape.PointingHandCursor)


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


class QmlPointerCursorState:
    """Reconcile generic and nested shaped cursors for one QQuickWidget."""

    def __init__(self, qml_widget: QWidget) -> None:
        self._qml_widget = qml_widget
        self._pointer_depth = 0
        self._shaped_cursors: dict[str, int] = {}

    def enter(self) -> None:
        self._pointer_depth += 1
        if not self._shaped_cursors:
            begin_qml_pointer_cursor(self._qml_widget)

    def enter_shaped(self, cursor_source: str, cursor_shape: int) -> None:
        self._shaped_cursors.pop(cursor_source, None)
        self._shaped_cursors[cursor_source] = cursor_shape
        set_qml_pointer_cursor(self._qml_widget, cursor_shape)

    def update_shaped(self, cursor_source: str, cursor_shape: int) -> None:
        if cursor_source not in self._shaped_cursors:
            return
        self._shaped_cursors[cursor_source] = cursor_shape
        if next(reversed(self._shaped_cursors)) == cursor_source:
            set_qml_pointer_cursor(self._qml_widget, cursor_shape)

    def exit_shaped(self, cursor_source: str) -> None:
        self._shaped_cursors.pop(cursor_source, None)
        self._apply_current()

    def exit(self) -> None:
        self._pointer_depth = max(0, self._pointer_depth - 1)
        self._apply_current()

    def reset(self) -> None:
        self._pointer_depth = 0
        self._shaped_cursors.clear()
        end_qml_pointer_cursor(self._qml_widget)

    def _apply_current(self) -> None:
        if self._shaped_cursors:
            active_source = next(reversed(self._shaped_cursors))
            set_qml_pointer_cursor(
                self._qml_widget,
                self._shaped_cursors[active_source],
            )
        elif self._pointer_depth > 0:
            begin_qml_pointer_cursor(self._qml_widget)
        else:
            end_qml_pointer_cursor(self._qml_widget)
