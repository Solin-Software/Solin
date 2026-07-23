"""Filesystem policy for media downloads and persistent cache files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

from solin.core.foundation.constants import TEMP_STREAM_PREFIX

PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.20
PROGRESS_EMIT_MIN_BYTES = 512 * 1024

log = logging.getLogger(__name__)

_CACHE_COMMIT_LOCK = threading.RLock()
_LOCK_SESSION_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex}"
_SAFE_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


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
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{digest}{_safe_extension(url)}")


def is_url_cached(url: str, media_cache_dir: str | os.PathLike[str]) -> bool:
    if not is_remote_url(url):
        return False
    try:
        return completed_cached_path(url, media_cache_dir) is not None
    except (OSError, UnicodeError, ValueError):
        return False


def completed_cached_path(
    url: str,
    media_cache_dir: str | os.PathLike[str],
) -> str | None:
    if not is_remote_url(url):
        return None
    with _CACHE_COMMIT_LOCK:
        path = cached_path_for(url, media_cache_dir)
        if _cache_entry_matches(path, url):
            return path
        return _migrate_legacy_cache_entry(url, media_cache_dir, path)


def prepare_download_target(
    url: str,
    media_cache_dir: str | os.PathLike[str],
    *,
    persist: bool,
) -> DownloadTarget:
    with _CACHE_COMMIT_LOCK:
        if persist:
            final_path = cached_path_for(url, media_cache_dir)
            completed_path = completed_cached_path(url, media_cache_dir)
            if completed_path is not None:
                return DownloadTarget(
                    final_path=completed_path,
                    write_path=completed_path,
                    persist=True,
                    cached_size=os.path.getsize(completed_path),
                )
            write_path = make_persistent_temp_path(final_path)
            acquire_temp_lock(write_path)
            return DownloadTarget(
                final_path=final_path,
                write_path=write_path,
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
    try:
        with _CACHE_COMMIT_LOCK:
            marker_path = target.final_path + ".done"
            _remove_file_if_exists(marker_path)
            _sync_file(target.write_path)
            os.replace(target.write_path, target.final_path)
            _write_marker_atomically(marker_path, url)
    finally:
        release_temp_lock(target.write_path)


def _safe_extension(url: str) -> str:
    try:
        suffix = os.path.splitext(urlsplit(url).path)[1]
    except ValueError:
        return ""
    return suffix.lower() if _SAFE_EXTENSION_RE.fullmatch(suffix) else ""


def _cache_entry_matches(path: str, url: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path + ".done", "r", encoding="utf-8") as marker:
            return marker.read() == url
    except (OSError, UnicodeError):
        return False


def _legacy_cached_path_for(
    url: str,
    media_cache_dir: str | os.PathLike[str],
) -> str | None:
    filename = filename_from_url(url)
    if (
        not filename
        or filename in {".", ".."}
        or os.path.basename(filename) != filename
    ):
        return None
    return os.path.join(os.fspath(media_cache_dir), filename)


def _migrate_legacy_cache_entry(
    url: str,
    media_cache_dir: str | os.PathLike[str],
    destination: str,
) -> str | None:
    legacy_path = _legacy_cached_path_for(url, media_cache_dir)
    if (
        legacy_path is None
        or os.path.normcase(os.path.abspath(legacy_path))
        == os.path.normcase(os.path.abspath(destination))
        or not _cache_entry_matches(legacy_path, url)
    ):
        return None

    destination_marker = destination + ".done"
    legacy_marker = legacy_path + ".done"
    try:
        _remove_file_if_exists(destination_marker)
    except OSError:
        return None
    try:
        os.replace(legacy_path, destination)
        try:
            _write_marker_atomically(destination_marker, url)
        except (OSError, UnicodeError, ValueError):
            os.replace(destination, legacy_path)
            raise
        safe_remove(legacy_marker)
    except (OSError, UnicodeError, ValueError):
        if _cache_entry_matches(destination, url):
            return destination
        return None
    return destination


def _sync_file(path: str | os.PathLike[str]) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/os.fsync.
    with open(path, "r+b") as handle:
        os.fsync(handle.fileno())


def _remove_file_if_exists(path: str | os.PathLike[str]) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _write_marker_atomically(path: str, url: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as marker:
            marker.write(url)
            marker.flush()
            os.fsync(marker.fileno())
        os.replace(temp_path, path)
    except (OSError, UnicodeError, ValueError):
        try:
            os.close(fd)
        except OSError:
            pass
        safe_remove(temp_path)
        raise


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
            handle.write(_LOCK_SESSION_TOKEN)
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

    with _CACHE_COMMIT_LOCK:
        try:
            entries = os.listdir(tmp_dir)
        except OSError:
            return 0

        for name in entries:
            if not (name.startswith(TEMP_STREAM_PREFIX) and name.endswith(".lock")):
                continue

            found_lock_path = os.path.join(tmp_dir, name)
            if _lock_is_active(found_lock_path):
                continue
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
    with _CACHE_COMMIT_LOCK:
        try:
            entries = os.listdir(cache_dir)
        except OSError:
            return 0

        for name in entries:
            if name.endswith((".done", ".lock")):
                continue

            path = os.path.join(cache_dir, name)
            if not os.path.isfile(path):
                continue
            active_lock = path + ".lock"
            if os.path.isfile(active_lock) and _lock_is_active(active_lock):
                continue

            if name.endswith(".tmp") or not os.path.isfile(path + ".done"):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    continue
                release_temp_lock(path)

        for name in entries:
            if not name.endswith(".lock"):
                continue
            found_lock_path = os.path.join(cache_dir, name)
            if _lock_is_active(found_lock_path):
                continue
            try:
                os.remove(found_lock_path)
            except OSError:
                pass

    if removed:
        log.info("Removed %d incomplete media cache download(s).", removed)
    return removed


def _lock_is_active(path: str | os.PathLike[str]) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except (OSError, UnicodeError):
        return False
    if token == _LOCK_SESSION_TOKEN:
        return True
    try:
        owner_pid = int(token.partition(":")[0])
    except ValueError:
        return False
    if owner_pid <= 0 or owner_pid == os.getpid():
        return False
    return _process_is_running(owner_pid)


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
