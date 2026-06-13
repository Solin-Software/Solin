from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


class ApplicationLifecycle:
    def __init__(self, app) -> None:
        self._app = app
        self._cleanup_callbacks: list[Callable[[], None]] = []
        self._installed = False
        self._shutdown_started = False

    def install(self) -> None:
        if self._installed:
            return
        self._app.aboutToQuit.connect(self.shutdown)
        self._installed = True

    def register_cleanup(self, callback: Callable[[], None]) -> None:
        self._cleanup_callbacks.append(callback)

    def register_single_instance(self, server) -> None:
        self.register_cleanup(server.close)

    def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True

        while self._cleanup_callbacks:
            callback = self._cleanup_callbacks.pop()
            try:
                callback()
            except Exception:  # noqa: BLE001 - application shutdown boundary
                log.warning("Application shutdown cleanup failed", exc_info=True)
