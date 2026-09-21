"""Portable manifest helpers for linked folders."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, TypeVar

log = logging.getLogger(__name__)

MANIFEST_FILE = "_solin_manifest.json"
CACHE_DIR_NAME = ".solin_cache"
_LOCKS_GUARD = threading.Lock()
_FOLDER_LOCKS: dict[str, threading.RLock] = {}


class ManifestError(RuntimeError):
    """Raised when a linked-folder manifest cannot be trusted."""


class ManifestWriteError(ManifestError):
    """A classified failure while publishing a linked-folder manifest."""

    def __init__(
        self,
        path: Path,
        *,
        operation: str,
        retryable: bool,
        cause: BaseException,
    ) -> None:
        self.path = path
        self.operation = operation
        self.retryable = retryable
        self.winerror = getattr(cause, "winerror", None)
        self.errno = getattr(cause, "errno", None)
        super().__init__(f"Could not update {path.name}.")
        self.__cause__ = cause


ManifestMutation = Callable[[dict[str, Any]], bool | None]
T = TypeVar("T")


def retry_manifest_write(
    operation: Callable[[], T],
    *,
    timeout_seconds: float = 5.0,
) -> T:
    """Retry a classified transient write outside the GUI thread."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    delays = (0.05, 0.1, 0.25, 0.5, 1.0)
    attempt = 0
    while True:
        try:
            return operation()
        except ManifestWriteError as exc:
            if not exc.retryable or time.monotonic() >= deadline:
                raise
            delay = min(delays[min(attempt, len(delays) - 1)], deadline - time.monotonic())
            if delay <= 0:
                raise
            attempt += 1
            time.sleep(delay)


def _folder_lock(folder: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(folder))
    with _LOCKS_GUARD:
        return _FOLDER_LOCKS.setdefault(key, threading.RLock())


def _lock_name(folder: Path) -> str:
    import hashlib

    normalized = os.path.normcase(os.path.abspath(folder))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@contextmanager
def _interprocess_lock(folder: Path, *, timeout: float = 2.0) -> Iterator[None]:
    """Serialize local Solin processes without creating files in Dropbox."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, f"Local\\Solin.Manifest.{_lock_name(folder)}")
        if not handle:
            cause = ctypes.WinError(ctypes.get_last_error())
            raise ManifestWriteError(
                folder / MANIFEST_FILE,
                operation="lock",
                retryable=True,
                cause=cause,
            )
        wait_result = kernel32.WaitForSingleObject(handle, max(0, int(timeout * 1000)))
        try:
            if wait_result not in (0x00000000, 0x00000080):
                cause = TimeoutError("Timed out waiting for the local manifest lock")
                raise ManifestWriteError(
                    folder / MANIFEST_FILE,
                    operation="lock",
                    retryable=True,
                    cause=cause,
                )
            yield
        finally:
            if wait_result in (0x00000000, 0x00000080):
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    lock_root = Path(tempfile.gettempdir()) / "solin-manifest-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{_lock_name(folder)}.lock"
    with lock_path.open("a+b") as lock_file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ManifestWriteError(
                        folder / MANIFEST_FILE,
                        operation="lock",
                        retryable=True,
                        cause=exc,
                    ) from exc
                time.sleep(0.025)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def empty_manifest() -> dict[str, Any]:
    return {"version": 1, "processed": {}}


def to_manifest_url(url: str, subfolder: Path) -> str:
    """Convert a local URL under *subfolder* to a portable relative path."""
    if not url or url.startswith(("http://", "https://")):
        return url
    try:
        rel = Path(url).relative_to(subfolder)
        return rel.as_posix()
    except ValueError:
        return url


def is_absolute_local_url(url: str) -> bool:
    """Return True for local absolute paths from the current OS or Windows."""

    if not url or url.startswith(("http://", "https://")):
        return False
    return Path(url).is_absolute() or PureWindowsPath(url).is_absolute()


def absolute_local_url_tail(url: str) -> Path:
    """Return the portable tail for an absolute local URL from any supported OS."""

    current_path = Path(url)
    if current_path.is_absolute():
        parts = current_path.parts
    else:
        parts = PureWindowsPath(url).parts
    if len(parts) >= 2 and parts[-2] == CACHE_DIR_NAME:
        return Path(CACHE_DIR_NAME) / parts[-1]
    if parts:
        return Path(parts[-1])
    return Path("")


def from_manifest_url(url: str, subfolder: Path) -> str:
    """Resolve a manifest URL back to an absolute path on this machine."""
    if not url or url.startswith(("http://", "https://")):
        return url

    p = Path(url)
    if is_absolute_local_url(url):
        if p.exists():
            return str(p)
        return str(subfolder / absolute_local_url_tail(url))

    return str(subfolder / p)


def cache_dir(subfolder: Path) -> Path:
    """Return the .solin_cache directory inside *subfolder*, creating it."""
    d = subfolder / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class ManifestRepository:
    """Transactional persistence for all linked-folder manifest namespaces."""

    def load(self, subfolder: Path, *, strict: bool = False) -> dict[str, Any]:
        """Load and validate a manifest without mutating the filesystem."""

        return self.load_frozen(subfolder, strict=strict)[0]

    def load_frozen(
        self, subfolder: Path, *, strict: bool = False,
    ) -> tuple[dict[str, Any], bytes | None]:
        """Return validated content and its exact bytes from one filesystem read.

        Migration backups must use these bytes: a cloud provider can replace
        the source as soon as this read finishes. Missing files return no bytes.
        """

        mf = subfolder / MANIFEST_FILE
        try:
            original = mf.read_bytes()
            data = json.loads(original.decode("utf-8"))
            if not isinstance(data, dict):
                raise TypeError("manifest root must be an object")
            if not isinstance(data.get("processed"), dict):
                data["processed"] = {}
            data.setdefault("version", 1)
            return data, original
        except FileNotFoundError:
            return empty_manifest(), None
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            if strict:
                raise ManifestError(f"Invalid linked-folder manifest: {mf}") from exc
            log.warning("Manifest corrupted in %s; will re-process", subfolder)
            return empty_manifest(), None

    def update(
        self,
        subfolder: Path,
        mutation: ManifestMutation,
        *,
        strict: bool = True,
    ) -> dict[str, Any]:
        """Apply one read-modify-write transaction to the latest manifest."""

        subfolder = Path(subfolder)
        lock = _folder_lock(subfolder)
        with lock, _interprocess_lock(subfolder):
            manifest = self.load(subfolder, strict=strict)
            changed = mutation(manifest)
            if changed is False:
                return manifest
            manifest.setdefault("version", 1)
            self._publish(subfolder, manifest)
            return manifest

    def delete(self, subfolder: Path) -> None:
        """Delete the complete manifest under the same transaction locks."""

        subfolder = Path(subfolder)
        target = subfolder / MANIFEST_FILE
        with _folder_lock(subfolder), _interprocess_lock(subfolder):
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                raise ManifestWriteError(
                    target,
                    operation="delete",
                    retryable=getattr(exc, "winerror", None) in {5, 32, 33},
                    cause=exc,
                ) from exc

    def _publish(self, subfolder: Path, manifest: dict[str, Any]) -> None:
        target = subfolder / MANIFEST_FILE
        temp_path: Path | None = None
        operation = "prepare"
        try:
            subfolder.mkdir(parents=True, exist_ok=True)
            operation = "serialize"
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=subfolder,
                prefix=f".{MANIFEST_FILE}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(manifest, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                operation = "fsync"
                os.fsync(temp_file.fileno())
            operation = "replace"
            os.replace(temp_path, target)
            temp_path = None
        except (OSError, TypeError, ValueError) as exc:
            retryable = (
                operation in {"lock", "replace"}
                and getattr(exc, "winerror", None) in {5, 32, 33}
            )
            error = ManifestWriteError(
                target,
                operation=operation,
                retryable=retryable,
                cause=exc,
            )
            log.error(
                "Cannot write manifest to %s during %s (retryable=%s): %s",
                target,
                operation,
                retryable,
                exc,
            )
            raise error from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "Cannot remove temporary manifest %s",
                        temp_path,
                        exc_info=True,
                    )


MANIFEST_REPOSITORY = ManifestRepository()
