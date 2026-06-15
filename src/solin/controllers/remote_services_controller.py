from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from ..core.foundation.constants import NOTIFICATION_CHECK_DELAY_MS, UPDATE_CHECK_DELAY_MS


class SignalLike(Protocol):
    def connect(self, slot: Callable[..., object]) -> object: ...


class RemoteNotificationService(Protocol):
    notifications_ready: SignalLike

    def check(self) -> None: ...

    def stop(self, *, wait_ms: int = 0, delete_when_stopped: bool = False) -> None: ...


class RemoteUpdateService(Protocol):
    update_available: SignalLike

    def check(self) -> None: ...

    def stop(self, *, wait_ms: int = 0, delete_when_stopped: bool = False) -> None: ...


class RemoteNotificationQueue(Protocol):
    def enqueue(self, notifications: list[object]) -> None: ...


class UpdateDialogPresenter(Protocol):
    def show(self) -> None: ...


class RemoteServicesController:
    """Starts and stops delayed remote notification/update checks."""

    def __init__(
        self,
        parent: QWidget,
        *,
        notification_service: RemoteNotificationService,
        notification_queue: RemoteNotificationQueue,
        update_service: RemoteUpdateService,
        update_dialog_factory: Callable[[object], UpdateDialogPresenter],
        timer_factory: Callable[[QWidget], QTimer] = QTimer,
    ) -> None:
        self._notification_service = notification_service
        self._notification_queue = notification_queue
        self._notification_timer = timer_factory(parent)
        self._update_service = update_service
        self._update_dialog_factory = update_dialog_factory
        self._update_timer = timer_factory(parent)

    def start(self) -> None:
        self._notification_service.notifications_ready.connect(self._notification_queue.enqueue)
        self._notification_timer.setSingleShot(True)
        self._notification_timer.timeout.connect(self._notification_service.check)
        self._notification_timer.start(NOTIFICATION_CHECK_DELAY_MS)

        self._update_service.update_available.connect(self._on_update_available)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._update_service.check)
        self._update_timer.start(UPDATE_CHECK_DELAY_MS)

    def stop(self) -> None:
        for timer in (self._notification_timer, self._update_timer):
            self._stop_timer(timer)
        for service in (self._notification_service, self._update_service):
            self._stop_service(service)

    def _on_update_available(self, info) -> None:
        self._update_dialog_factory(info).show()

    @staticmethod
    def _stop_timer(timer) -> None:
        if timer:
            timer.stop()

    @staticmethod
    def _stop_service(service) -> None:
        if service and hasattr(service, "stop"):
            service.stop(wait_ms=100, delete_when_stopped=True)
