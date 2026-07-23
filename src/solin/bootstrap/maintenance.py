from __future__ import annotations

import logging
import threading
from typing import Any

from solin.core.foundation.thread_workers import ThreadedWorkerPool


log = logging.getLogger(__name__)


class DeferredApplicationMaintenance:
    """Runs installation/cache housekeeping outside the startup UI thread."""

    def __init__(self, installation_settings: Any, runtime_paths: Any) -> None:
        self._installation_settings = installation_settings
        self._runtime_paths = runtime_paths
        self._workers = ThreadedWorkerPool()
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._workers.submit("application-maintenance", self._run)

    def shutdown(self) -> None:
        self._workers.shutdown(timeout=1.5)

    def _run(self) -> None:
        try:
            from solin.core.media.download_storage import (
                cleanup_incomplete_cache,
                cleanup_orphan_temps,
            )
            from solin.core.remote.patch_installer import cleanup_pending_patch

            cleanup_pending_patch(self._installation_settings)
            cleanup_orphan_temps()
            cleanup_incomplete_cache(self._runtime_paths.media_cache_dir)
        except Exception:  # noqa: BLE001 - best-effort maintenance boundary
            log.warning("Deferred application maintenance failed", exc_info=True)


__all__ = ["DeferredApplicationMaintenance"]
