"""
notifications.py
================
Asynchronous Solin notification service.

Workflow:
  1. Start NotificationWorker in a separate QThread ~1.5 s after opening the app.
  2. GET NOTIFICATION_API_URL with a short timeout, without blocking the UI.
  3. Validate the payload's minimum schema.
  4. Filter notifications whose IDs are already in the profile's local history.
  5. Request content in the active language's api_code. The server resolves
     the requested language, English, then the first available content.
     The client retains the same defensive resolution for older backends.
  6. Emit `notifications_ready` with the processed list.
  7. MainWindow connects this signal and queues non-modal dialogs.

Security:
  - HTTPS and certificate verification through the central HTTP adapter.
  - Short FETCH_TIMEOUT_S to avoid delaying startup.
  - No sensitive data sent to the server.
  - Mark IDs as seen BEFORE emitting to prevent redisplay if a crash occurs
    between emission and user interaction.
  - Fully defensive parsing; exceptions are suppressed and logged.

Expected API format:
  {
    "version": 1,
    "notifications": [
      {
        "id": "2025_001",  // unique, immutable string
        "type": "info",  // "info" | "warning" | "error"
        "content": {
          "E": {"title": "...", "detail": "..."},  // English (fallback)
          "T": {"title": "...", "detail": "..."},  // Portuguese
          "S": {"title": "...", "detail": "..."}  // Spanish
          // … other api_codes as needed
        },
        "action": {  // OPTIONAL
          "url": "https://...",
          "label": "Learn more"  // optional button text; default if absent
        }
      }
    ]
  }
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from solin.core.i18n.manager import LanguageManager

from solin.core.foundation.constants import (
    APP_PLATFORM,
    APP_VERSION,
    NOTIFICATION_API_URL,
)
from solin.core.network.http import HttpError, get_json
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.notification_policy import Notification, resolve_remote_notifications
from solin.core.remote.notification_settings import NotificationSettingsStore

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S: int = 8        # total HTTP request timeout
InstallIdProvider = Callable[[], str]


# Asynchronous worker

class NotificationWorker(QObject):
    """
    Run in a separate QThread. Perform the HTTP request and emit the result.
    Do not access widgets; emit signals only.
    """
    notifications_ready = Signal(list)   # list[Notification]
    fetch_failed = Signal(str)           # error message for quiet logging

    def __init__(
        self,
        api_code: str,
        settings: NotificationSettingsStore,
        install_id_provider: InstallIdProvider,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        # active language api_code (e.g. "T" for Portuguese, "E" for English)
        self._api_code = api_code
        self._settings = settings
        self._install_id_provider = install_id_provider

    def run(self) -> None:
        """Called by the thread: fetch, process, and emit the result."""
        try:
            # Send id + v so the server can upsert AppInstance
            # and track active instances without an extra dedicated
            # request. Both fields are optional on the server.
            params = {
                "id":       self._install_id_provider(),
                "v":        _notification_version(),
                "platform": APP_PLATFORM,
                "lang":     self._api_code,
            }
            payload = get_json(
                NOTIFICATION_API_URL,
                params=params,
                timeout=FETCH_TIMEOUT_S,
                headers={
                    "Accept":     "application/json",
                    "User-Agent": f"Solin/{APP_VERSION}",
                },
            )
        except HttpError as exc:
            log.debug("[Notifications] fetch failed: %s", exc)
            self.fetch_failed.emit(str(exc))
            return

        notifications = self._process(payload)
        self.notifications_ready.emit(notifications)

    def _process(self, payload: object) -> list[Notification]:
        """
        Validate the payload, filter seen notifications, and resolve localized content.
        Return a list of Notification objects ready for display.
        """
        result = resolve_remote_notifications(
            payload,
            api_code=self._api_code,
            seen_ids=self._settings.seen_ids(),
        )
        for notification_id in result.mark_seen_ids:
            self._settings.mark_seen(notification_id)
        return list(result.notifications)


# Public controller

class NotificationService(QObject):
    """
    Public facade managing the thread lifecycle and exposing a clean signal.

    Typical usage:
        self._notif_service = NotificationService(
            lang_manager,
            notification_settings_store,
            install_id_provider,
            self,
        )
        self._notif_service.notifications_ready.connect(self._on_notifications)
        QTimer.singleShot(1500, self._notif_service.check)
    """
    notifications_ready = Signal(list)   # list[Notification]

    def __init__(
        self,
        lang_manager: "LanguageManager",
        notification_settings: NotificationSettingsStore,
        install_id_provider: InstallIdProvider,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._lang = lang_manager
        self._notification_settings = notification_settings
        self._install_id_provider = install_id_provider
        self._thread: QThread | None = None
        self._worker: NotificationWorker | None = None
        self._running = False
        self._stopping = False
        self._delete_when_stopped = False

    def check(self) -> None:
        """Start the asynchronous check. Ignore duplicate calls."""
        if self._stopping or self._running:
            return
        self._running = True

        api_code = self._lang.api_code  # ex: "T", "E", "S"

        self._thread = QThread(self)
        self._worker = NotificationWorker(
            api_code,
            self._notification_settings,
            self._install_id_provider,
        )
        self._worker.moveToThread(self._thread)

        # Conecta sinais
        self._thread.started.connect(self._worker.run)
        self._worker.notifications_ready.connect(self._on_ready)
        self._worker.fetch_failed.connect(self._on_failed)
        self._worker.notifications_ready.connect(self._thread.quit)
        self._worker.fetch_failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)

        self._thread.start()

    def stop(self, wait_ms: int = 0, delete_when_stopped: bool = False) -> None:
        """
        Stop the thread lifecycle safely.

        The ongoing HTTP request may be blocked outside Qt's event loop.
        If it does not finish within the deadline, detach the service from its
        parent so the window can close without destroying an active QThread.
        """
        self._stopping = True
        self._delete_when_stopped = self._delete_when_stopped or delete_when_stopped

        if self._thread and self._thread.isRunning():
            self._thread.quit()
            if wait_ms > 0:
                self._thread.wait(wait_ms)

        if self._thread and self._thread.isRunning() and self._delete_when_stopped:
            self.setParent(None)
        elif not self._running and self._delete_when_stopped:
            self.deleteLater()

    def shutdown(self) -> None:
        """Compatibility for callers expecting shutdown()."""
        self.stop(wait_ms=3000, delete_when_stopped=True)

    # ── Slots privados ─────────────────────────────────────────────────────────

    def _on_ready(self, notifications: list) -> None:
        if self._stopping:
            return
        if notifications:
            log.debug("[Notifications] %d new notification(s)", len(notifications))
            self.notifications_ready.emit(notifications)

    def _on_failed(self, msg: str) -> None:
        log.debug("[Notifications] silent failure: %s", msg)
        self._running = False

    def _cleanup(self) -> None:
        """Release thread resources after completion."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
        self._running = False
        if self._delete_when_stopped:
            self.deleteLater()


def _notification_version() -> str:
    version = ReleaseVersion.parse(APP_VERSION)
    if version is None:
        raise ValueError("Invalid application version")
    return version.notification_version
