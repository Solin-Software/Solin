from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

from ..core.foundation.constants import NOTIFICATION_CHECK_DELAY_MS, UPDATE_CHECK_DELAY_MS
from ..core.remote.notifications import NotificationService
from ..core.remote.updates import UpdateService
from ..widgets.notification_dialog import NotificationQueue
from ..widgets.update_dialog import UpdateDialog

if TYPE_CHECKING:
    from solin.core.i18n.manager import LanguageManager
    from solin.main_window import MainWindow


class RemoteServicesController:
    """Starts and stops delayed remote notification/update checks."""

    def __init__(self, window: MainWindow, lang_manager: LanguageManager) -> None:
        self._window = window
        self._notification_service = NotificationService(
            lang_manager,
            window.profile_settings,
            window,
        )
        self._notification_queue = NotificationQueue(lang_manager, window)
        self._notification_timer = QTimer(window)
        self._update_service = UpdateService(window)
        self._update_timer = QTimer(window)

    def start(self) -> None:
        self._notification_service.notifications_ready.connect(
            self._notification_queue.enqueue
        )
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
        dlg = UpdateDialog(info, self._window)
        dlg.show()

    @staticmethod
    def _stop_timer(timer) -> None:
        if timer:
            timer.stop()

    @staticmethod
    def _stop_service(service) -> None:
        if service and hasattr(service, "stop"):
            service.stop(wait_ms=100, delete_when_stopped=True)
