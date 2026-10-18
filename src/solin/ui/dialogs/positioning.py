"""Screen-aware positioning for dialogs owned by windows or embedded widgets."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog


def centered_dialog_position(anchor: QRect, dialog_size: QSize, available: QRect) -> QPoint:
    """Center the dialog frame on its owner, constrained to the owner's screen."""
    x = anchor.x() + (anchor.width() - dialog_size.width()) // 2
    y = anchor.y() + (anchor.height() - dialog_size.height()) // 2
    max_x = available.right() - dialog_size.width() + 1
    max_y = available.bottom() - dialog_size.height() + 1
    return QPoint(
        max(available.left(), min(x, max_x)),
        max(available.top(), min(y, max_y)),
    )


def center_dialog_on_parent(dialog: QDialog) -> None:
    """Position a visible dialog after Qt has resolved its layout and native frame."""
    if not dialog.isVisible():
        return

    parent = dialog.parentWidget()
    parent_window = parent.window() if parent is not None else None
    if parent_window is not None and parent_window.isVisible():
        anchor = parent_window.frameGeometry()
        screen = parent_window.screen()
    else:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        anchor = screen.availableGeometry()

    available = screen.availableGeometry() if screen is not None else anchor
    frame_size = dialog.frameGeometry().size()
    if frame_size.isEmpty():
        frame_size = dialog.size()
    dialog.move(centered_dialog_position(anchor, frame_size, available))
