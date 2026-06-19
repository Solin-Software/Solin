"""Filesystem policy for media downloads and persistent cache files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import tempfile
import time

from solin.core.foundation.constants import TEMP_STREAM_PREFIX

PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.20
PROGRESS_EMIT_MIN_BYTES = 512 * 1024

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadTarget:
    final_path: str
    write_path: str
    persist: bool
    cached_size: int | None = None

    @property
    def is_cached(self) -> bool:
        return self.cached_size is not None


@dataclass
class DownloadProgressGate:
    min_interval_seconds: float = PROGRESS_EMIT_MIN_INTERVAL_SECONDS
    min_bytes: int = PROGRESS_EMIT_MIN_BYTES
    clock: Callable[[], float] = time.monotonic
    last_progress_at: float = 0.0
    last_progress_pct: int = -1
    last_progress_bytes: int = 0

    def should_emit(
        self,
        downloaded: int,
        total: int,
        *,
        force: bool = False,
        cancelled: bool = False,
    ) -> bool:
        if downloaded <= 0 or cancelled:
            return False

        now = self.clock()
        if total > 0:
            pct = int(downloaded * 100 / total)
            if force or pct != self.last_progress_pct:
                self.last_progress_pct = pct
                self.last_progress_at = now
                self.last_progress_bytes = downloaded
                return True
            return False

        if (
            force
            or self.last_progress_at <= 0
            or downloaded - self.last_progress_bytes >= self.min_bytes
            or now - self.last_progress_at >= self.min_interval_seconds
        ):
            self.last_progress_at = now
            self.last_progress_bytes = downloaded
            return True
        return False


def filename_from_url(url: str) -> str:
    return url.split("/")[-1].split("?")[0]


def is_remote_url(url: str) -> bool:
    return bool(url and url.startswith("http"))


def cached_path_for(url: str, media_cache_dir: str | os.PathLike[str]) -> str:
    cache_dir = os.fspath(media_cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, filename_from_url(url))


def is_url_cached(url: str, media_cache_dir: str | os.PathLike[str]) -> bool:
    if not is_remote_url(url):
        return False
    try:
        path = cached_path_for(url, media_cache_dir)
        return os.path.isfile(path) and os.path.isfile(path + ".done")
    except (OSError, ValueError):
        return False


def completed_cached_path(
    url: str,
    media_cache_dir: str | os.PathLike[str],
) -> str | None:
    if not is_remote_url(url):
        return None
    path = cached_path_for(url, media_cache_dir)
    if os.path.exists(path) and os.path.exists(path + ".done"):
        return path
    return None


def prepare_download_target(
    url: str,
    media_cache_dir: str | os.PathLike[str],
    *,
    persist: bool,
) -> DownloadTarget:
    if persist:
        final_path = cached_path_for(url, media_cache_dir)
        if os.path.exists(final_path) and os.path.exists(final_path + ".done"):
            return DownloadTarget(
                final_path=final_path,
                write_path=final_path,
                persist=True,
                cached_size=os.path.getsize(final_path),
            )
        return DownloadTarget(
            final_path=final_path,
            write_path=make_persistent_temp_path(final_path),
            persist=True,
        )

    write_path = make_stream_temp_path(url)
    acquire_temp_lock(write_path)
    return DownloadTarget(
        final_path=write_path,
        write_path=write_path,
        persist=False,
    )


def commit_persistent_download(target: DownloadTarget, url: str) -> None:
    if not target.persist:
        return
    os.replace(target.write_path, target.final_path)
    with open(target.final_path + ".done", "w", encoding="utf-8") as marker:
        marker.write(url)


def make_stream_temp_path(url: str) -> str:
    name = filename_from_url(url)
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    fd, path = tempfile.mkstemp(suffix=ext, prefix=TEMP_STREAM_PREFIX)
    os.close(fd)
    return path


def make_persistent_temp_path(final_path: str | os.PathLike[str]) -> str:
    final = os.fspath(final_path)
    directory = os.path.dirname(final)
    filename = os.path.basename(final)
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=directory,
    )
    os.close(fd)
    return path


def lock_path(temp_path: str | os.PathLike[str]) -> str:
    return os.fspath(temp_path) + ".lock"


def acquire_temp_lock(temp_path: str | os.PathLike[str]) -> None:
    try:
        with open(lock_path(temp_path), "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass


def release_temp_lock(temp_path: str | os.PathLike[str]) -> None:
    try:
        path = lock_path(temp_path)
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def safe_remove(path: str | os.PathLike[str] | None) -> None:
    if path:
        target = os.fspath(path)
        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
        release_temp_lock(target)


def cleanup_orphan_temps() -> int:
    tmp_dir = tempfile.gettempdir()
    removed = 0

    try:
        entries = os.listdir(tmp_dir)
    except OSError:
        return 0

    for name in entries:
        if not (name.startswith(TEMP_STREAM_PREFIX) and name.endswith(".lock")):
            continue

        found_lock_path = os.path.join(tmp_dir, name)
        temp_path = found_lock_path[: -len(".lock")]

        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
                removed += 1
            os.remove(found_lock_path)
        except OSError:
            pass

    if removed:
        log.info("Removed %d orphan stream tempfile(s).", removed)
    return removed


def cleanup_incomplete_cache(
    media_cache_dir: str | os.PathLike[str],
) -> int:
    cache_dir = os.fspath(media_cache_dir)
    if not os.path.isdir(cache_dir):
        return 0

    removed = 0
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return 0

    for name in entries:
        if name.endswith(".done"):
            continue

        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue

        if name.endswith(".tmp") or not os.path.isfile(path + ".done"):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass

    if removed:
        log.info("Removed %d incomplete media cache download(s).", removed)
    return removed
