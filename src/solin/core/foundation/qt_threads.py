from __future__ import annotations

import logging
from typing import Any


def stop_owned_qthread(
    thread: Any,
    *,
    wait_ms: int,
    logger: logging.Logger | None = None,
    label: str = "Background",
) -> bool:
    """Request shutdown for an owned QThread and wait for its completion."""
    if thread is None:
        return True
    try:
        if not thread.isRunning():
            return True
        request_interruption = getattr(thread, "requestInterruption", None)
        if request_interruption is not None:
            request_interruption()
        thread.quit()
        thread.wait(wait_ms)
        if not thread.isRunning():
            return True
        if logger is not None:
            logger.warning(
                "%s thread %s did not stop within %d ms",
                label,
                type(thread).__name__,
                wait_ms,
            )
        thread.setParent(None)
        thread.finished.connect(thread.deleteLater)
        return False
    except RuntimeError:
        return True


class OwnedQThreadRegistry:
    """Tracks transient QThreads and drains them during application shutdown."""

    def __init__(self) -> None:
        self._threads: list[Any] = []

    def track(self, thread: Any) -> Any:
        if thread not in self._threads:
            self._threads.append(thread)
            thread.finished.connect(
                lambda thread_ref=thread: self.discard(thread_ref)
            )
        return thread

    def discard(self, thread: Any) -> None:
        if thread in self._threads:
            self._threads.remove(thread)

    def stop_all(
        self,
        *,
        wait_ms: int = 3_000,
        logger: logging.Logger | None = None,
        label: str = "Conversion",
    ) -> None:
        for thread in list(self._threads):
            stopped = stop_owned_qthread(
                thread,
                wait_ms=wait_ms,
                logger=logger,
                label=label,
            )
            if stopped:
                self.discard(thread)

    @property
    def active_count(self) -> int:
        return len(self._threads)
