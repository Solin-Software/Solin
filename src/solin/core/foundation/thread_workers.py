from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class CancellationFlag:
    """Thread-safe cancellation flag shared by UI owners and worker callbacks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._gate = threading.RLock()

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        with self._gate:
            self._event.set()

    def run_if_active(self, action: Callable[[], None]) -> bool:
        """Run one atomic side effect unless cancellation already won the gate."""

        with self._gate:
            if self._event.is_set():
                return False
            action()
            return True


class WorkerHandle(Protocol):
    @property
    def name(self) -> str:
        ...

    def is_alive(self) -> bool:
        ...

    def is_current(self) -> bool:
        ...

    def join(self, timeout: float | None = None) -> None:
        ...


class WorkerPool(Protocol):
    def submit(
        self,
        name: str,
        target: Callable[[], None],
    ) -> WorkerHandle | None:
        ...


@dataclass(frozen=True, slots=True)
class ThreadWorkerHandle:
    _thread: threading.Thread

    @property
    def name(self) -> str:
        return self._thread.name

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def is_current(self) -> bool:
        return self._thread is threading.current_thread()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)


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

    def submit(
        self,
        name: str,
        target: Callable[[], None],
    ) -> ThreadWorkerHandle | None:
        if self._stop.is_set():
            return None

        def _run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=_run, daemon=True, name=name)
        with self._lock:
            if self._stop.is_set():
                return None
            self._threads.add(thread)
        thread.start()
        return ThreadWorkerHandle(thread)

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
