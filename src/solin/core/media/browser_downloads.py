"""Browser download workflows and persistent cache storage."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
import urllib.parse
import urllib.request

from solin.core.media.download_storage import (
    DownloadTarget,
    cached_path_for,
    commit_persistent_download,
    completed_cached_path,
    filename_from_url,
    make_persistent_temp_path,
    safe_remove,
)
from solin.core.media.formats import IMAGE_EXTS
from solin.core.network.http import HttpError, stream_get

log = logging.getLogger(__name__)

DownloadReady = Callable[[str, str, str], None]
DownloadFailed = Callable[[str, str], None]
StreamFactory = Callable[..., Any]


class BrowserDownloadService:
    """Owns browser download threads, HTTP transfer, and cache persistence."""

    _USER_AGENT = "Mozilla/5.0 (compatible; Solin/1.0)"

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        *,
        notify_cached: Callable[[str], None],
        stream_factory: StreamFactory = stream_get,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._notify_cached = notify_cached
        self._stream_factory = stream_factory
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._shutdown = False
        self._sequence = 0

    def destination_cache_path(self, url: str, title: str, kind: str) -> Path:
        ext_by_kind = {
            "pdf": ".pdf",
            "jwpub": ".jwpub",
            "jwlplaylist": ".jwlplaylist",
            "image": ".jpg",
        }
        parsed = urllib.parse.urlparse(url)
        raw_name = urllib.parse.unquote(Path(parsed.path).name or title or "download")
        suffix = Path(raw_name).suffix.lower()
        if kind == "image" and suffix not in IMAGE_EXTS:
            suffix = ext_by_kind[kind]
        elif not suffix:
            suffix = ext_by_kind.get(kind, "")
        stem = Path(raw_name).stem or Path(title).stem or "download"
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" ._")
        if not stem:
            stem = "download"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        return self._cache_dir / f"{stem[:80]}-{digest}{suffix}"

    def download_file_for_destination(
        self,
        url: str,
        title: str,
        kind: str,
        *,
        on_ready: DownloadReady,
        on_failed: DownloadFailed,
    ) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            on_ready(urllib.request.url2pathname(parsed.path), title, kind)
            return
        if parsed.scheme not in {"http", "https"}:
            return

        destination = self.destination_cache_path(url, title, kind)
        if self._is_completed(destination, url):
            on_ready(os.fspath(destination), title, kind)
            return

        def work() -> None:
            try:
                self._download(url, destination, timeout=45, chunk_size=131_072)
                on_ready(os.fspath(destination), title, kind)
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Download for playlist failed "%s": %s', title, exc)
                on_failed(title, str(exc))

        self._start("browser-destination-download", work)

    def cache_media(self, url: str) -> None:
        if not self._is_remote(url):
            return

        filename = filename_from_url(url) or "media_file"
        destination = Path(cached_path_for(url, self._cache_dir))
        if completed_cached_path(url, self._cache_dir) is not None:
            return

        def work() -> None:
            try:
                self._download(url, destination, timeout=30, chunk_size=131_072)
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Cache save failed for "%s": %s', filename, exc)

        self._start("browser-cache-download", work)

    def download_image_for_destination(
        self,
        url: str,
        title: str,
        *,
        on_ready: DownloadReady,
        on_failed: DownloadFailed,
    ) -> None:
        if not self._is_remote(url):
            return

        destination = self.destination_cache_path(url, title, "image")
        if self._is_completed(destination, url):
            on_ready(os.fspath(destination), title, "image")
            return

        def work() -> None:
            try:
                self._download(url, destination, timeout=30, chunk_size=65_536)
                on_ready(os.fspath(destination), title, "image")
            except (HttpError, OSError, ValueError) as exc:
                log.warning('Image download for playlist failed "%s": %s', title, exc)
                on_failed(title, str(exc))

        self._start("browser-image-download", work)

    def shutdown(self, timeout: float = 2.0) -> list[str]:
        with self._lock:
            self._shutdown = True
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

    def _download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: int,
        chunk_size: int,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_path = make_persistent_temp_path(destination)
        try:
            with self._stream_factory(
                url,
                timeout=timeout,
                headers={"User-Agent": self._USER_AGENT},
            ) as response:
                with open(write_path, "wb") as handle:
                    for chunk in response.iter_bytes(chunk_size):
                        handle.write(chunk)
            commit_persistent_download(
                DownloadTarget(
                    final_path=os.fspath(destination),
                    write_path=write_path,
                    persist=True,
                ),
                url,
            )
            self._notify_cached(url)
        except (HttpError, OSError, ValueError):
            safe_remove(write_path)
            raise

    def _start(self, name: str, work: Callable[[], None]) -> bool:
        with self._lock:
            if self._shutdown:
                return False
            self._sequence += 1
            thread = threading.Thread(
                target=lambda: self._run_worker(work),
                daemon=True,
                name=f"{name}-{self._sequence}",
            )
            self._threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._threads.discard(thread)
            raise
        return True

    def _run_worker(self, work: Callable[[], None]) -> None:
        try:
            work()
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    @staticmethod
    def _is_remote(url: str) -> bool:
        return urllib.parse.urlparse(url).scheme in {"http", "https"}

    @staticmethod
    def _is_completed(destination: Path, url: str) -> bool:
        marker = destination.with_name(destination.name + ".done")
        if not destination.is_file() or not marker.is_file():
            return False
        try:
            return marker.read_text(encoding="utf-8") == url
        except (OSError, UnicodeError):
            return False
