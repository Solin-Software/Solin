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
    finished: SignalLike
    installation_stopped: SignalLike

    def show(self) -> None: ...

    def invalidate(self) -> bool:
        """Cancel/close obsolete UI, or return False once installation starts."""
        ...


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
        self._update_dialog: UpdateDialogPresenter | None = None
        self._dialog_invalidated = False
        self._recheck_after_installation = False
        self._pending_update: object | None = None
        self._started = False
        self._stopping = False

    def start(self) -> None:
        if self._started or self._stopping:
            return
        self._started = True
        self._notification_service.notifications_ready.connect(self._notification_queue.enqueue)
        self._notification_timer.setSingleShot(True)
        self._notification_timer.timeout.connect(self._notification_service.check)
        self._notification_timer.start(NOTIFICATION_CHECK_DELAY_MS)

        self._update_service.update_available.connect(self._on_update_available)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.check_updates)
        self._update_timer.start(UPDATE_CHECK_DELAY_MS)

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._recheck_after_installation = False
        self._pending_update = None
        for timer in (self._notification_timer, self._update_timer):
            self._stop_timer(timer)
        if self._update_dialog is not None:
            self._update_dialog.invalidate()
        for service in (self._notification_service, self._update_service):
            self._stop_service(service)

    def check_updates(self) -> None:
        """Use one coordinator for startup and preference-triggered checks."""
        if self._stopping:
            return
        self._recheck_after_installation = False
        self._update_timer.stop()
        self._pending_update = None
        if self._update_dialog is not None and not self._dialog_invalidated:
            self._dialog_invalidated = True
            if not self._update_dialog.invalidate():
                self._dialog_invalidated = False
                self._recheck_after_installation = True
                return
        self._update_service.check()

    def _on_update_available(self, info: object) -> None:
        if self._stopping:
            return
        if self._update_dialog is not None:
            if self._dialog_invalidated:
                # Some presenters close asynchronously. Retain their reference
                # until finished, then present only the latest valid result.
                self._pending_update = info
            return
        dialog = self._update_dialog_factory(info)
        self._update_dialog = dialog
        dialog.finished.connect(lambda *_: self._on_update_dialog_finished(dialog))
        dialog.installation_stopped.connect(lambda: self._on_installation_stopped(dialog))
        dialog.show()

    def _on_installation_stopped(self, dialog: UpdateDialogPresenter) -> None:
        if self._update_dialog is dialog and self._recheck_after_installation:
            self.check_updates()

    def _on_update_dialog_finished(self, dialog: UpdateDialogPresenter) -> None:
        if self._update_dialog is not dialog:
            return
        self._update_dialog = None
        self._dialog_invalidated = False
        if self._recheck_after_installation:
            self.check_updates()
            return
        pending, self._pending_update = self._pending_update, None
        if pending is not None:
            self._on_update_available(pending)

    @staticmethod
    def _stop_timer(timer) -> None:
        if timer:
            timer.stop()

    @staticmethod
    def _stop_service(service) -> None:
        if service and hasattr(service, "stop"):
            service.stop(wait_ms=0, delete_when_stopped=True)
