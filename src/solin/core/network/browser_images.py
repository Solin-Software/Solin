"""Generation-aware browser image fetching."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time

from solin.core.network.http import HttpError, get_bytes

log = logging.getLogger(__name__)


class BrowserImageFetchService:
    """Owns browser image-fetch generations and worker lifecycle."""

    _MAX_IMAGE_BYTES = 25 * 1024 * 1024

    def __init__(
        self,
        fetch_url: Callable[[str], bytes] | None = None,
    ) -> None:
        self._fetch_url = fetch_url or self._read_url
        self._lock = threading.Lock()
        self._generation = 0
        self._shutdown = False
        self._threads: set[threading.Thread] = set()

    @classmethod
    def _read_url(cls, url: str) -> bytes:
        return get_bytes(
            url,
            timeout=15,
            max_bytes=cls._MAX_IMAGE_BYTES,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    def claim(self) -> int | None:
        with self._lock:
            if self._shutdown:
                return None
            self._generation += 1
            return self._generation

    def start(
        self,
        url: str,
        deliver: Callable[[int, bytes], None],
    ) -> int | None:
        generation = self.claim()
        if generation is None:
            return None

        def fetch() -> None:
            try:
                payload = self._fetch_url(url)
                if self.is_current(generation):
                    deliver(generation, payload)
            except (HttpError, OSError, ValueError) as exc:
                log.warning("Image fetch error: %s", exc)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=fetch,
            daemon=True,
            name=f"browser-image-fetch-{generation}",
        )
        with self._lock:
            if self._shutdown or generation != self._generation:
                return None
            self._threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._threads.discard(thread)
            raise
        return generation

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return not self._shutdown and generation == self._generation

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1

    def shutdown(self, timeout: float = 2.0) -> list[str]:
        with self._lock:
            self._shutdown = True
            self._generation += 1
            threads = list(self._threads)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        return [thread.name for thread in threads if thread.is_alive()]
