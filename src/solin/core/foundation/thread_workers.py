from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ThreadedWorkerPool:
    """Owns a small set of daemon stdlib workers and drains them on shutdown."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

    @property
    def is_stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._threads)

    def submit(self, name: str, target: Callable[[], None]) -> None:
        if self._stop.is_set():
            return

        def _run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=_run, daemon=True, name=name)
        with self._lock:
            if self._stop.is_set():
                return
            self._threads.add(thread)
        thread.start()

    def shutdown(self, timeout: float = 8.0) -> tuple[str, ...]:
        self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = tuple(self._threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if thread is threading.current_thread():
                continue
            thread.join(timeout=remaining)
        alive = tuple(thread.name for thread in threads if thread.is_alive())
        with self._lock:
            self._threads = {thread for thread in self._threads if thread.is_alive()}
        return alive
