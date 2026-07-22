"""Frame-friendly scheduling for GUI-thread media probe requests."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import logging

from PySide6.QtCore import QObject, QTimer, Slot


logger = logging.getLogger(__name__)
_DEFAULT_BATCH_SIZE = 12
_DEFAULT_INTERVAL_MS = 8


class ProbeRequestQueue(QObject):
    """Coalesce node requests and dispatch a bounded batch per Qt turn."""

    def __init__(
        self,
        consumer: Callable[[str], None],
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        interval_ms: int = _DEFAULT_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if interval_ms < 0:
            raise ValueError("interval_ms must be non-negative")
        super().__init__(parent)
        self._consumer = consumer
        self._batch_size = batch_size
        self._pending: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.drain_now)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def enqueue(self, node_id: str, *, priority: bool = False) -> None:
        if self._closed or not node_id:
            return
        # Reassigning an OrderedDict key preserves its place, so ordinary
        # coalescing never demotes work that is already near the front.
        self._pending[node_id] = None
        if priority:
            self._pending.move_to_end(node_id, last=False)
        if not self._timer.isActive():
            self._timer.start()

    def discard(self, node_id: str) -> None:
        self._pending.pop(node_id, None)

    def clear(self) -> None:
        self._timer.stop()
        self._pending.clear()

    def close(self) -> None:
        self._closed = True
        self.clear()

    @Slot()
    def drain_now(self) -> None:
        if self._closed:
            return
        # Manual drains must replace the already-scheduled timeout. Otherwise
        # an empty callback survives after all work has been consumed.
        self._timer.stop()
        for _index in range(min(self._batch_size, len(self._pending))):
            node_id, _value = self._pending.popitem(last=False)
            try:
                self._consumer(node_id)
            except Exception:  # noqa: BLE001 - keep unrelated probes progressing
                logger.exception("Could not schedule media presentation probe for %s", node_id)
        if self._pending:
            self._timer.start()


__all__ = ["ProbeRequestQueue"]
