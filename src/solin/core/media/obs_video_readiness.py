"""Native first-frame preparation in the OBS video thread."""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class ObsSourceVideoReadiness:
    """Observe a new source before its decoder can publish frames.

    OBS selects async frames during its tick and discards unconsumed frames on
    the next tick. Preparing in the same tick's render phase prevents that gap.
    The owner must close the observer before releasing or detaching the source.
    """

    def __init__(self, runtime: Any, source: Any) -> None:
        self._runtime = runtime
        self._source = source
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._failed = False
        self._awaiting_update = False
        self._disconnect_update = runtime.observe_source_updates(source, self._updated)
        try:
            self._callback = runtime.ob.add_main_render_callback(self._prepare)
        except Exception:  # noqa: BLE001 - roll back partial native registration
            self._disconnect_update()
            raise

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def updating(self) -> bool:
        with self._lock:
            return self._awaiting_update

    def invalidate(self) -> None:
        """Require another decoded frame after a transport/decoder reset."""
        with self._lock:
            self._ready.clear()
            self._failed = False

    def update(self, settings: dict[str, object]) -> None:
        """Gate preparation until the plugin acknowledges the deferred update."""
        with self._lock:
            source = self._source
            if source is None:
                raise RuntimeError("Cannot update a closed video readiness observer")
            self._ready.clear()
            self._failed = False
            self._awaiting_update = True
        try:
            source.update(settings)
        except Exception:  # noqa: BLE001 - a rejected update cannot remain pending
            with self._lock:
                self._awaiting_update = False
                self._failed = True
            raise

    def _updated(self) -> None:
        with self._lock:
            if self._source is None:
                return
            try:
                # async_tick selects before process_media_actions/deferred_update.
                # Its current frame therefore belongs to the preceding decoder.
                self._runtime.discard_selected_source_video(self._source)
                self._ready.clear()
                self._failed = False
                self._awaiting_update = False
            except Exception:  # noqa: BLE001 - native signal callback boundary
                self._failed = True
                log.warning("Could not acknowledge native source update", exc_info=True)
            finally:
                self._awaiting_update = False

    def _prepare(self, _width: int, _height: int) -> None:
        with self._lock:
            if self._source is None or self._awaiting_update or self._ready.is_set() or self._failed:
                return
            try:
                if self._runtime.prime_source_video(self._source):
                    self._ready.set()
            except Exception:  # noqa: BLE001 - report failures at the native callback boundary
                self._failed = True
                log.warning("Could not prepare native source video", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._source = None
            self._ready.clear()
            callback, self._callback = self._callback, None
            disconnect, self._disconnect_update = self._disconnect_update, None
        # Unregistration waits for native callbacks. Do not hold their lock here.
        try:
            if callback is not None:
                try:
                    self._runtime.ob.remove_main_render_callback(callback)
                except Exception:  # noqa: BLE001 - retain the handle for cleanup retry
                    with self._lock:
                        self._callback = callback
                    raise
        finally:
            if disconnect is not None:
                try:
                    disconnect()
                except Exception:  # noqa: BLE001 - keep the CFFI callback alive until disconnected
                    with self._lock:
                        self._disconnect_update = disconnect
                    raise
