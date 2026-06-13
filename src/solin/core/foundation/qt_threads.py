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
