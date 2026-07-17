from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QSize, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractButton, QComboBox


class NoScrollComboBox(QComboBox):
    """Ignore wheel events to avoid accidental selection changes."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class ButtonConfirmationFeedback(QObject):
    """Show a stable, transient confirmation state without changing button geometry."""

    def __init__(
        self,
        button: QAbstractButton,
        *,
        idle_icon: Callable[[], QIcon],
        confirmed_icon: Callable[[], QIcon],
        icon_size: int = 14,
        duration_ms: int = 1600,
    ) -> None:
        super().__init__(button)
        self._button = button
        self._idle_icon = idle_icon
        self._confirmed_icon = confirmed_icon
        self._icon_size = QSize(icon_size, icon_size)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(duration_ms)
        self._timer.timeout.connect(self.reset)
        self.reset()

    @property
    def confirmed(self) -> bool:
        return self._timer.isActive()

    def confirm(self) -> None:
        self._timer.start()
        self._apply_state(True)

    def reset(self) -> None:
        self._timer.stop()
        self._apply_state(False)

    def apply_theme(self) -> None:
        self._apply_state(self.confirmed)

    def _apply_state(self, confirmed: bool) -> None:
        self._button.setProperty("confirmed", confirmed)
        self._button.setIcon(self._confirmed_icon() if confirmed else self._idle_icon())
        self._button.setIconSize(self._icon_size)
        style = self._button.style()
        style.unpolish(self._button)
        style.polish(self._button)
        self._button.update()
