from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt


_DISPATCH_EVENT_TYPE = QEvent.Type(QEvent.registerEventType())


class CooperativeDispatch(QObject):
    """Run one callback in a low-priority event-loop turn."""

    def __init__(self, callback: Callable[[], None], parent: QObject) -> None:
        super().__init__(parent)
        self._callback = callback
        self._pending = False

    def request(self) -> None:
        if self._pending:
            return
        self._pending = True
        QCoreApplication.postEvent(
            self,
            QEvent(_DISPATCH_EVENT_TYPE),
            Qt.EventPriority.LowEventPriority.value,
        )

    def cancel(self) -> None:
        self._pending = False

    def event(self, event: QEvent) -> bool:
        if event.type() != _DISPATCH_EVENT_TYPE:
            return super().event(event)
        if self._pending:
            self._pending = False
            self._callback()
        return True


__all__ = ["CooperativeDispatch"]
