"""Global, immutable storage for images extracted from publications."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock

from solin.core.media.cache_listing import CachedMediaItem
from solin.core.media.formats import IMAGE_EXTS
from solin.core.storage.binary_files import publish_bytes_immutable

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass
class _RootState:
    lock: RLock = field(default_factory=RLock)
    leases: Counter[str] = field(default_factory=Counter)
    owners: dict[str, frozenset[str]] = field(default_factory=dict)

    def pinned(self, identifier: str) -> bool:
        return self.leases[identifier] > 0 or any(
            identifier in paths for paths in self.owners.values()
        )


_ROOT_STATES: dict[Path, _RootState] = {}
_ROOT_STATES_LOCK = Lock()


class PublicationAssetStore:
    """Keep original image bytes once per SHA-256 digest.

    One immutable JSON sidecar preserves the first publisher's filename and
    title. Synchronization and temporary pins are shared by every instance for
    the same resolved root within this process. Persistent references remain
    the responsibility of the caller supplying a collection snapshot.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        with _ROOT_STATES_LOCK:
            self._state = _ROOT_STATES.setdefault(self._root, _RootState())

    @property
    def root(self) -> Path:
        return self._root

    def owns(self, path: str | Path) -> bool:
        """Recognize an object path without following links or requiring it to exist."""
        with self._state.lock:
            return self._owned_path(path) is not None

    def materialize(self, source: str | Path, *, title: str = "") -> str:
        """Publish original bytes atomically; existing bytes and metadata never change."""
        source_path = Path(source)
        if source_path.suffix.lower() not in IMAGE_EXTS:
            raise ValueError(f"Unsupported publication image extension: {source_path.suffix}")
        if not isinstance(title, str):
            raise TypeError("Publication image title must be a string")
        with self._state.lock:
            self._check_root()
            payload = source_path.read_bytes()
            identifier = hashlib.sha256(payload).hexdigest()
            self._root.mkdir(parents=True, exist_ok=True)
            metadata_path = self._root / f"{identifier}.json"
            if not metadata_path.exists() and not self._is_link(metadata_path):
                if self._has_image_entry(identifier):
                    raise OSError(
                        errno.ENOENT, "Publication asset metadata is missing", str(metadata_path)
                    )
                metadata = json.dumps(
                    {"filename": source_path.name, "title": title.strip()},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                try:
                    # Metadata precedes bytes so its failure cannot expose an
                    # unlisted object. An interrupted data write is retryable.
                    publish_bytes_immutable(metadata_path, metadata)
                except FileExistsError:
                    # A different process may have published the same digest
                    # with another filename or title; its metadata wins.
                    self._read_metadata(identifier)
            filename, _title = self._read_metadata(identifier)
            target = self._root / f"{identifier}{Path(filename).suffix.lower()}"
            if self._owned_path(target) is None:
                raise OSError(errno.EPERM, "Unsafe publication asset path", str(target))
            try:
                existing = target.read_bytes()
            except FileNotFoundError:
                publish_bytes_immutable(target, payload)
            else:
                if existing != payload:
                    raise FileExistsError(
                        errno.EEXIST, "Conflicting publication asset bytes", str(target)
                    )
            return str(target)

    def asset_id(self, path: str | Path) -> str:
        """Return the digest encoded by a safe object path, including a missing object."""
        with self._state.lock:
            target = self._owned_path(path)
            if target is None:
                raise ValueError(f"Not a publication asset path: {path}")
            return target.stem

    @contextmanager
    def lease(self, paths: Collection[str]) -> Iterator[None]:
        """Protect existing or planned asset paths, or original image bytes.

        Leasing original image sources before materialization bridges publication
        to owner retention. Sources must remain unchanged during that handoff.
        """
        with self._state.lock:
            identifiers = self._pin_ids(paths, include_sources=True)
            self._state.leases.update(identifiers)
        try:
            yield
        finally:
            with self._state.lock:
                for identifier in identifiers:
                    self._state.leases[identifier] -= 1
                    if not self._state.leases[identifier]:
                        del self._state.leases[identifier]

    def retain(self, owner: str, paths: Collection[str]) -> None:
        """Atomically replace an owner's in-flight pins; foreign media paths are ignored."""
        self._validate_owner(owner)
        with self._state.lock:
            identifiers = self._pin_ids(paths)
            if identifiers:
                self._state.owners[owner] = identifiers
            else:
                self._state.owners.pop(owner, None)

    def release(self, owner: str) -> None:
        """Release an owner's pins without affecting other owners or active leases."""
        self._validate_owner(owner)
        with self._state.lock:
            self._state.owners.pop(owner, None)

    def remove(self, path: str | Path) -> bool:
        """Remove owned bytes, preserving their repair identity until reference GC.

        Raise OSError for pins or unreadable metadata. The sidecar is a small
        tombstone: another publication may name identical bytes differently.
        """
        with self._state.lock:
            target = self._owned_path(path)
            if target is None:
                return False
            if self._state.pinned(target.stem):
                raise OSError(errno.EBUSY, "Publication asset is in use", str(target))
            if not target.exists():
                return False
            with self._removal_directory() as directory:
                filename, _title = self._read_metadata(target.stem)
                if target.suffix != Path(filename).suffix.lower():
                    raise OSError(
                        errno.EINVAL, "Publication asset metadata disagrees with its path"
                    )
                if self._owned_path(target, strict=True) is None:
                    raise OSError(errno.EPERM, "Unsafe publication asset path", str(target))
                metadata_path = self._root / f"{target.stem}.json"
                self._check_regular_file(metadata_path)
                self._unlink_entry(target, directory)
            return True

    def list_items(self) -> list[CachedMediaItem]:
        """List complete objects, excluding unsafe paths and invalid or missing metadata."""
        with self._state.lock:
            self._check_root()
            if not self._root.exists():
                return []
            items = []
            for path in sorted(self._root.iterdir()):
                if self._owned_path(path) is None:
                    continue
                try:
                    filename, title = self._read_metadata(path.stem)
                    if path.suffix != Path(filename).suffix.lower():
                        continue
                    size = path.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                items.append(
                    CachedMediaItem(
                        path=str(path),
                        filename=filename,
                        display_title=title or Path(filename).stem,
                        size=size,
                        media_type="image",
                    )
                )
            return items

    def collect(
        self, reference_provider: Callable[[], set[str]], *, retain_images: bool = False
    ) -> list[str]:
        """Remove unreferenced, unpinned objects using a synchronized snapshot.

        References may be SHA-256 identifiers or absolute local paths. Foreign
        absolute paths are ignored. Provider exceptions and malformed snapshots
        propagate before any deletion; invalid object metadata is left intact.
        Valid, unreferenced metadata without an image is also cleaned up; only
        removed media paths are returned.
        With retain_images, complete images survive but unreferenced tombstones
        are still reclaimed after their last persistent consumer disappears.
        The provider must not wait for another thread to enter this store.
        """
        with self._state.lock:
            references = self._reference_ids(reference_provider())
            self._check_root()
            if not self._root.exists():
                return []
            protected = references | self._state.leases.keys()
            for owner_paths in self._state.owners.values():
                protected.update(owner_paths)
            removed = []
            metadata_paths = []
            for path in sorted(self._root.iterdir()):
                identifier = path.stem
                if identifier in protected or not _DIGEST.fullmatch(identifier):
                    continue
                if path.suffix == ".json":
                    metadata_paths.append(path)
                    continue
                if retain_images:
                    continue
                if path.suffix not in IMAGE_EXTS or self._owned_path(path) is None:
                    continue
                try:
                    filename, _title = self._read_metadata(identifier)
                except OSError:
                    continue
                if path.suffix == Path(filename).suffix.lower() and self.remove(path):
                    removed.append(str(path))
            self._collect_orphaned_metadata(metadata_paths)
            return removed

    def _collect_orphaned_metadata(self, paths: Collection[Path]) -> None:
        for metadata_path in paths:
            identifier = metadata_path.stem
            with self._removal_directory() as directory:
                try:
                    self._read_metadata(identifier)
                    if self._has_image_entry(identifier):
                        continue
                    self._check_root()
                    self._check_regular_file(metadata_path)
                except OSError:
                    continue
                self._unlink_entry(metadata_path, directory)

    def _has_image_entry(self, identifier: str) -> bool:
        for suffix in IMAGE_EXTS:
            try:
                (self._root / f"{identifier}{suffix}").stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            return True
        return False

    @staticmethod
    def _unlink_entry(path: Path, directory: int | None) -> None:
        if directory is None:
            path.unlink()
        else:
            os.unlink(path.name, dir_fd=directory)

    def _check_root(self) -> None:
        if self._is_link(self._root) or self._root.resolve() != self._root:
            raise OSError(errno.EPERM, "Publication asset root contains a link", str(self._root))

    @contextmanager
    def _removal_directory(self) -> Iterator[int | None]:
        """Anchor POSIX unlinks; prevent directory replacement on Windows."""
        if os.name != "nt":
            descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self._check_root()
                yield descriptor
            finally:
                os.close(descriptor)
            return

        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT. Omitting
        # FILE_SHARE_DELETE keeps this directory from being moved or replaced.
        handle = kernel.CreateFileW(str(self._root), 0x80000000, 3, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._check_root()
            yield None
        finally:
            kernel.CloseHandle(handle)

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _check_regular_file(self, path: Path) -> None:
        if self._is_link(path) or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise OSError(errno.EPERM, "Publication asset must be a regular file", str(path))

    def _owned_path(self, path: str | Path, *, strict: bool = False) -> Path | None:
        try:
            target = self._asset_path(path)
            if target is None:
                return None
            self._check_root()
            if self._is_link(target):
                if strict:
                    raise OSError(errno.EPERM, "Publication asset path is a link", str(target))
                return None
            try:
                mode = target.stat(follow_symlinks=False).st_mode
            except FileNotFoundError:
                return target
            if stat.S_ISREG(mode):
                return target
            if strict:
                raise OSError(errno.EPERM, "Publication asset is not a regular file", str(target))
            return None
        except (OSError, ValueError, TypeError):
            if strict:
                raise
            return None

    def _asset_path(self, path: str | Path) -> Path | None:
        target = Path(path).absolute()
        if (
            target.parent == self._root
            and _DIGEST.fullmatch(target.stem)
            and target.suffix in IMAGE_EXTS
        ):
            return target
        return None

    def _read_metadata(self, identifier: str) -> tuple[str, str]:
        path = self._root / f"{identifier}.json"
        try:
            self._check_regular_file(path)
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or set(data) != {"filename", "title"}:
                raise ValueError("Invalid metadata fields")
            filename, title = data["filename"], data["title"]
            if (
                not isinstance(filename, str)
                or not filename
                or "/" in filename
                or "\\" in filename
                or "\x00" in filename
                or Path(filename).suffix.lower() not in IMAGE_EXTS
                or not isinstance(title, str)
            ):
                raise ValueError("Invalid publication image metadata")
            return filename, title
        except (ValueError, UnicodeError, RecursionError) as error:
            raise OSError(errno.EINVAL, "Invalid publication asset metadata", str(path)) from error

    def _pin_ids(
        self,
        paths: Collection[str],
        *,
        include_sources: bool = False,
    ) -> frozenset[str]:
        if isinstance(paths, (str, bytes)) or not isinstance(paths, Collection):
            raise TypeError("Publication asset pins must be a collection of paths")
        identifiers = set()
        for path in paths:
            if not isinstance(path, (str, Path)):
                raise TypeError("Publication asset pins must contain local paths")
            target = self._owned_path(path, strict=True)
            if target is not None:
                identifiers.add(target.stem)
            elif include_sources and Path(path).suffix.lower() in IMAGE_EXTS:
                identifiers.add(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        return frozenset(identifiers)

    def _reference_ids(self, references: set[str]) -> set[str]:
        if not isinstance(references, set):
            raise TypeError("Publication asset reference provider must return a set")
        identifiers = set()
        for reference in references:
            if not isinstance(reference, str) or not reference or "\x00" in reference:
                raise ValueError("Invalid publication asset reference")
            if _DIGEST.fullmatch(reference):
                identifiers.add(reference)
                continue
            path = Path(reference)
            if not path.is_absolute():
                raise ValueError("Publication asset references must be digests or absolute paths")
            if path.is_relative_to(self._root):
                target = self._asset_path(path)
                if target is None:
                    raise ValueError("Invalid publication asset reference path")
                identifiers.add(target.stem)
        return identifiers

    @staticmethod
    def _validate_owner(owner: str) -> None:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Publication asset pin owner must be a nonempty string")
