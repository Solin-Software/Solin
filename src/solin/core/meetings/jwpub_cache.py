from __future__ import annotations

import hashlib
import json
import logging
import lzma
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from weakref import WeakValueDictionary

from solin.core.storage.json_repository import JsonFileRepository

log = logging.getLogger(__name__)

_EXTRACTION_MARKER = ".solin-extraction-complete"
_EXTRACTION_STALE_MARKER = ".solin-extraction-stale"
_SCRATCH_MARKER = ".solin-extraction-transient"
_MAX_TEMP_BYTES = 2 * 1024**3
_MAX_RETAINED_BYTES = 512 * 1024**2
_COPY_CHUNK_SIZE = 1024 * 1024
_Publication = tuple[str, str, str]


@dataclass
class _Operation:
    protected: set[Path]
    entries: set[_Publication] = field(default_factory=set)
    scratch: dict[_Publication, Path] = field(default_factory=dict)
    created: set[Path] = field(default_factory=set)


class _RootState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.operations: list[_Operation] = []


_ROOT_STATES: WeakValueDictionary[str, _RootState] = WeakValueDictionary()
_ROOT_STATES_LOCK = threading.Lock()
_CHECKSUM_STORE_LOCK = threading.Lock()


def _root_state(root: Path) -> _RootState:
    key = os.path.normcase(str(root))
    with _ROOT_STATES_LOCK:
        state = _ROOT_STATES.get(key)
        if state is None:
            state = _RootState()
            _ROOT_STATES[key] = state
        return state


class JwpubCache:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._state = _root_state(self._root)

    @contextmanager
    def operation(self, protected_sources: Collection[str] = ()) -> Iterator[JwpubCache]:
        """Lease this root until images are materialized, then discard new scratch.

        Instances sharing a root serialize operations in this process. Nested
        operations share leases. Existing legacy extracts are never removed or
        replaced by an operation; fresh extractions use separate scratch paths.
        Protected files and directories preserve intersecting extraction trees.
        This is opt-in: callers must finish reading extracted files before exit.
        Unprotected archives larger than 512 MiB are not retained after their
        final lease. Protection can temporarily exceed retention limits.
        """
        with self._state.lock:
            operation = _Operation(self._protected_paths(protected_sources))
            for active in self._state.operations:
                active.protected.update(operation.protected)
            self._state.operations.append(operation)
            try:
                yield self
            finally:
                self._state.operations.pop()
                for scratch in operation.created:
                    if any(scratch in active.created for active in self._state.operations):
                        continue
                    if not self._contains_protected(scratch, operation.protected):
                        self._remove_extract_dir(scratch)
                for key in operation.entries:
                    if any(key in active.entries for active in self._state.operations):
                        continue
                    try:
                        archive, legacy_extract = self._paths(key)
                        protected = self._contains_protected(legacy_extract, operation.protected)
                        if (
                            not protected
                            and archive.is_file()
                            and archive.stat().st_size > _MAX_RETAINED_BYTES
                        ):
                            archive.unlink()
                        else:
                            self._touch_archive(archive)
                    except (OSError, ValueError) as exc:
                        log.warning("JwpubCache: could not clean operation entry %s: %s", key, exc)

    def prune(
        self,
        protected_sources: Collection[str] = (),
        preferred_publications: Collection[_Publication] = (),
        max_bytes: int = _MAX_RETAINED_BYTES,
        max_age_seconds: int = 30 * 86400,
    ) -> tuple[_Publication, ...]:
        """Discard unleased extracts and expired/oldest archives; return removed keys.

        Preferred publications are evicted last when enforcing the byte budget.
        Expiry still applies to them. Protected sources preserve their owning
        extract and archive, including missing sources awaiting legacy repair.
        Active/protected packages may exceed the budget. Never runs implicitly.
        """
        if max_bytes < 0 or max_age_seconds < 0:
            raise ValueError("Cache limits must be nonnegative")
        with self._state.lock:
            protected = self._protected_paths(protected_sources)
            leased: set[_Publication] = set()
            for active in self._state.operations:
                protected.update(active.protected)
                leased.update(active.entries)
            self._prune_scratch(protected)
            preferred = set(preferred_publications)
            cutoff = time.time() - max_age_seconds
            candidates: list[tuple[_Publication, Path, int, float]] = []
            total = 0
            for key in self._entries():
                archive, extract_path = self._paths(key)
                if key in leased or self._contains_protected(extract_path, protected):
                    try:
                        total += archive.stat().st_size
                    except FileNotFoundError:
                        continue
                    continue
                self._remove_extract_dir(extract_path)
                try:
                    info = archive.stat()
                except FileNotFoundError:
                    continue
                total += info.st_size
                candidates.append((key, archive, info.st_size, info.st_mtime))
            candidates.sort(
                key=lambda entry: (entry[3] >= cutoff, entry[0] in preferred, entry[3], entry[0])
            )
            removed: list[_Publication] = []
            for key, archive, size, modified in candidates:
                if modified >= cutoff and total <= max_bytes:
                    continue
                try:
                    archive.unlink()
                except OSError as exc:
                    log.warning("JwpubCache: could not prune archive %s: %s", archive, exc)
                    continue
                total -= size
                removed.append(key)
            return tuple(removed)

    def _paths(self, key: _Publication) -> tuple[Path, Path]:
        pub, lang, issue = key
        for value in key:
            _validate_component(value)
        directory = self._root / f"{pub}_{lang}"
        archive = directory / f"{pub}_{lang}_{issue}.jwpub"
        extract_path = directory / f"x_{issue}"
        if not self._owns(archive) or not self._owns(extract_path):
            raise ValueError("Cache entries must not traverse symbolic links")
        return archive, extract_path

    def _owns(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self._root)
            current = self._root
            for part in relative.parts:
                current /= part
                if current.is_symlink() or current.is_junction():
                    return False
            return path.resolve().is_relative_to(self._root)
        except (OSError, ValueError):
            return False

    def _entries(self) -> set[_Publication]:
        entries: set[_Publication] = set()
        for directory in self._root.iterdir():
            if not self._owns(directory) or not directory.is_dir() or "_" not in directory.name:
                continue
            pub, lang = directory.name.rsplit("_", 1)
            prefix = f"{directory.name}_"
            for path in directory.iterdir():
                issue = ""
                if path.name.startswith(prefix) and path.suffix == ".jwpub" and path.is_file():
                    issue = path.stem[len(prefix) :]
                elif path.name.startswith("x_") and path.is_dir():
                    issue = path.name[2:]
                if not issue:
                    continue
                key = (pub, lang, issue)
                try:
                    self._paths(key)
                except ValueError:
                    continue
                entries.add(key)
        return entries

    def _record(self, key: _Publication, archive: Path) -> None:
        newly_used = False
        for operation in self._state.operations:
            if key not in operation.entries:
                operation.entries.add(key)
                newly_used = True
        if newly_used:
            self._touch_archive(archive)

    def _prune_scratch(self, protected: set[Path]) -> None:
        leased = {path for active in self._state.operations for path in active.created}
        for path in self._root.glob("*/.x_*.*"):
            marker = path / _SCRATCH_MARKER
            if (
                path not in leased
                and self._owns(marker)
                and marker.is_file()
                and not self._contains_protected(path, protected)
            ):
                self._remove_extract_dir(path)

    @staticmethod
    def _touch_archive(archive: Path) -> None:
        try:
            os.utime(archive, None)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("JwpubCache: could not update archive access time %s: %s", archive, exc)

    @staticmethod
    def _protected_paths(sources: Collection[str]) -> set[Path]:
        paths: set[Path] = set()
        for source in sources:
            if not source:
                continue
            path = Path(source).absolute()
            paths.add(path)
            try:
                paths.add(path.resolve())
            except (OSError, ValueError):
                log.warning("JwpubCache: could not resolve protected source %s", source)
        return paths

    @staticmethod
    def _contains_protected(extract_path: Path, protected: set[Path]) -> bool:
        return any(
            source.is_relative_to(extract_path) or extract_path.is_relative_to(source)
            for source in protected
        )

    def jwpub_path(self, pub: str, lang: str, issue: str) -> Path:
        with self._state.lock:
            archive, _ = self._paths((pub, lang, issue))
            archive.parent.mkdir(exist_ok=True)
            self._record((pub, lang, issue), archive)
            return archive

    def extract_dir(self, pub: str, lang: str, issue: str) -> Path:
        with self._state.lock:
            key = (pub, lang, issue)
            archive, extract_path = self._paths(key)
            self._record(key, archive)
            for active in reversed(self._state.operations):
                if key in active.scratch:
                    return active.scratch[key]
            return extract_path

    def is_cached(self, pub: str, lang: str, issue: str) -> bool:
        with self._state.lock:
            return self._is_cached(pub, lang, issue)

    def _is_cached(self, pub: str, lang: str, issue: str) -> bool:
        extract_path = self.extract_dir(pub, lang, issue)
        if self.db_path(pub, lang, issue) is None:
            return False
        if (extract_path / _EXTRACTION_STALE_MARKER).is_file():
            return False
        return (extract_path / _EXTRACTION_MARKER).is_file() or not self.jwpub_path(
            pub, lang, issue
        ).is_file()

    def can_materialize(self, pub: str, lang: str, issue: str) -> bool:
        """Return whether a publication can be rebuilt without a download."""

        with self._state.lock:
            return self.is_cached(pub, lang, issue) or self.jwpub_path(pub, lang, issue).is_file()

    def db_path(self, pub: str, lang: str, issue: str) -> Path | None:
        with self._state.lock:
            extract_path = self.extract_dir(pub, lang, issue)
            return next((path for path in extract_path.glob("*.db") if self._owns(path)), None)

    def invalidate_extract(self, pub: str, lang: str, issue: str) -> None:
        """
        Remove an extracted publication so the next read unpacks fresh contents.

        A missing directory is a no-op. Filesystem errors are logged and kept
        local because cache invalidation must never crash the meeting workflow.
        """
        with self._state.lock:
            extract_path = self.extract_dir(pub, lang, issue)
            if self._state.operations:
                self._mark_path_stale(extract_path)
            else:
                self._remove_extract_dir(extract_path)

    def mark_extract_stale(self, pub: str, lang: str, issue: str) -> None:
        """Require a fresh extraction without removing files currently in use."""

        with self._state.lock:
            self._mark_path_stale(self.extract_dir(pub, lang, issue))

    def repair_extract_for_source(self, source: str | os.PathLike[str]) -> bool:
        """Restore missing files from the owning archive without removing live files."""

        return self.repair_extracts_for_sources((source,)) > 0

    def repair_extracts_for_sources(
        self,
        sources: Iterable[str | os.PathLike[str]],
    ) -> int:
        """Repair each affected extraction once and return the successful count."""

        with self._state.lock:
            owners = {
                owner
                for source in sources
                if (owner := self._extract_owner_for_source(source)) is not None
            }
            return sum(self._repair_extract(extract_path, jwpub) for extract_path, jwpub in owners)

    def _repair_extract(self, extract_path: Path, jwpub: Path) -> bool:
        if not jwpub.is_file():
            self._mark_path_stale(extract_path)
            return False

        staging_path = self._stage_extract(jwpub, extract_path)
        if staging_path is None:
            self._mark_path_stale(extract_path)
            return False
        try:
            if not extract_path.exists():
                os.replace(staging_path, extract_path)
                staging_path = None
                return True

            for staged in staging_path.rglob("*"):
                relative = staged.relative_to(staging_path)
                if relative.name in {_EXTRACTION_MARKER, _EXTRACTION_STALE_MARKER}:
                    continue
                destination = extract_path / relative
                if not self._owns(destination):
                    raise OSError(f"Repair destination traverses a symbolic link: {destination}")
                if staged.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, destination)
            if not self._owns(extract_path / _EXTRACTION_MARKER):
                raise OSError("Extraction marker traverses a symbolic link")
            (extract_path / _EXTRACTION_MARKER).touch(exist_ok=True)
            (extract_path / _EXTRACTION_STALE_MARKER).unlink(missing_ok=True)
            log.info("JwpubCache: repaired missing files in %s", extract_path)
            return True
        except OSError as exc:
            log.error("JwpubCache: could not repair extract dir %s: %s", extract_path, exc)
            self._mark_path_stale(extract_path)
            return False
        finally:
            if staging_path is not None and staging_path.exists():
                self._cleanup_tree(staging_path, "repair staging dir")

    def _extract_owner_for_source(
        self,
        source: str | os.PathLike[str],
    ) -> tuple[Path, Path] | None:
        try:
            relative = Path(source).resolve().relative_to(self._root.resolve())
        except (OSError, ValueError):
            return None
        if len(relative.parts) < 2 or not relative.parts[1].startswith("x_"):
            return None
        publication_dir = self._root / relative.parts[0]
        extract_path = publication_dir / relative.parts[1]
        issue = relative.parts[1][2:]
        if "_" not in publication_dir.name:
            return None
        pub, lang = publication_dir.name.rsplit("_", 1)
        try:
            archive, extract_path = self._paths((pub, lang, issue))
        except ValueError:
            return None
        self._record((pub, lang, issue), archive)
        return extract_path, archive

    def _mark_path_stale(self, extract_path: Path) -> None:
        marker = extract_path / _EXTRACTION_STALE_MARKER
        if not extract_path.is_dir() or not self._owns(marker):
            return
        try:
            marker.touch(exist_ok=True)
        except OSError as exc:
            log.warning("JwpubCache: could not mark extract stale %s: %s", extract_path, exc)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return

    def _remove_extract_dir(self, extract_path: Path) -> bool:
        if (
            not self._owns(extract_path)
            or not extract_path.exists()
            or self._is_protected(extract_path)
        ):
            return False

        try:
            self._remove_tree(extract_path)
            log.debug("JwpubCache: invalidated extract dir %s", extract_path)
            return True
        except OSError as exc:
            log.error(
                "JwpubCache: could not remove extract dir %s: %s",
                extract_path,
                exc,
            )
            return False

    def _is_protected(self, extract_path: Path) -> bool:
        return any(
            self._contains_protected(extract_path, active.protected)
            for active in self._state.operations
        )

    def extract(self, pub: str, lang: str, issue: str) -> Path | None:
        with self._state.lock:
            return self._extract(pub, lang, issue)

    def _extract(self, pub: str, lang: str, issue: str) -> Path | None:
        jwpub = self.jwpub_path(pub, lang, issue)
        if not jwpub.exists():
            return None
        extract_path = self.extract_dir(pub, lang, issue)
        extract_path.parent.mkdir(parents=True, exist_ok=True)
        staging_target = (
            self._paths((pub, lang, issue))[1] if self._state.operations else extract_path
        )
        staging_path = self._stage_extract(jwpub, staging_target)
        if staging_path is None:
            return None
        if self._state.operations:
            try:
                (staging_path / _SCRATCH_MARKER).touch(exist_ok=False)
            except OSError as exc:
                log.error("JwpubCache: could not mark scratch extraction %s: %s", staging_path, exc)
                self._cleanup_tree(staging_path, "unmarked scratch extraction")
                return None
            for active in self._state.operations:
                active.scratch[(pub, lang, issue)] = staging_path
                active.created.add(staging_path)
            return staging_path
        previous_path: Path | None = None
        try:
            if extract_path.exists():
                previous_path = extract_path.parent / (
                    f".{extract_path.name}.previous-{uuid.uuid4().hex}"
                )
                os.replace(extract_path, previous_path)
            os.replace(staging_path, extract_path)
            staging_path = None
            return extract_path
        except OSError as exc:
            log.error("Extract failed %s: %s", jwpub, exc)
            if previous_path is not None and previous_path.exists() and not extract_path.exists():
                try:
                    os.replace(previous_path, extract_path)
                except OSError as restore_exc:
                    log.error(
                        "JwpubCache: could not restore extract dir %s: %s",
                        extract_path,
                        restore_exc,
                    )
            return None
        finally:
            if staging_path is not None and staging_path.exists():
                self._cleanup_tree(staging_path, "extraction staging dir")
            if previous_path is not None and previous_path.exists() and extract_path.exists():
                self._cleanup_tree(previous_path, "previous extract dir")

    def _stage_extract(self, jwpub: Path, extract_path: Path) -> Path | None:
        staging_path: Path | None = None
        complete = False
        try:
            staging_path = Path(
                tempfile.mkdtemp(prefix=f".{extract_path.name}.", dir=extract_path.parent)
            )
            with tempfile.TemporaryFile(dir=extract_path.parent) as payload:
                with zipfile.ZipFile(jwpub, "r") as outer:
                    contents = [info for info in outer.infolist() if info.filename == "contents"]
                    if len(contents) != 1:
                        raise RuntimeError("publication archive must have one contents payload")
                    member = contents[0]
                    _validate_member(member)
                    if member.is_dir() or member.file_size > _MAX_TEMP_BYTES:
                        raise RuntimeError("publication contents exceeds temporary disk budget")
                    with outer.open(member) as source:
                        payload_size = _copy_limited(source, payload, _MAX_TEMP_BYTES)
                payload.seek(0)
                with zipfile.ZipFile(payload, "r") as inner:
                    _safe_extract_all(inner, staging_path, _MAX_TEMP_BYTES - payload_size)
            if not any(staging_path.glob("*.db")):
                raise RuntimeError("publication has no database")
            (staging_path / _EXTRACTION_MARKER).touch(exist_ok=False)
            complete = True
            return staging_path
        except (
            EOFError,
            lzma.LZMAError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
        ) as exc:
            log.error("Extract failed %s: %s", jwpub, exc)
            return None
        finally:
            if not complete and staging_path is not None:
                self._cleanup_tree(staging_path, "failed extraction staging dir")

    def _cleanup_tree(self, path: Path, label: str) -> None:
        try:
            self._remove_tree(path)
        except OSError as exc:
            log.warning("JwpubCache: could not clean %s %s: %s", label, path, exc)


def _validate_component(component: str) -> None:
    stem = component.split(".", 1)[0].upper()
    reserved = stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved = reserved or (
        len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"
    )
    if (
        not component
        or component in {".", ".."}
        or component.endswith((".", " "))
        or reserved
        or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in component)
    ):
        raise ValueError(f"Unsafe archive/cache path component: {component!r}")


def _validate_member(member: zipfile.ZipInfo) -> tuple[str, ...]:
    name = member.filename.removesuffix("/") if member.is_dir() else member.filename
    parts = tuple(name.split("/"))
    if member.orig_filename != member.filename:
        raise ValueError("Archive member contains a NUL character")
    for part in parts:
        _validate_component(part)
    mode = stat.S_IFMT(member.external_attr >> 16)
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.external_attr & 0x400:
        raise ValueError(f"Archive member is a symbolic link or special file: {member.filename}")
    if (mode == stat.S_IFDIR and not member.is_dir()) or (
        member.is_dir() and (mode == stat.S_IFREG or member.file_size != 0)
    ):
        raise ValueError(f"Inconsistent archive directory: {member.filename}")
    if parts[0].casefold() in {_EXTRACTION_MARKER, _EXTRACTION_STALE_MARKER, _SCRATCH_MARKER}:
        raise ValueError("Archive member conflicts with extraction metadata")
    return parts


def _copy_limited(source: IO[bytes], destination: IO[bytes], max_bytes: int) -> int:
    copied = 0
    while chunk := source.read(min(_COPY_CHUNK_SIZE, max_bytes - copied + 1)):
        copied += len(chunk)
        if copied > max_bytes:
            raise RuntimeError("publication exceeds temporary disk budget")
        destination.write(chunk)
    return copied


def _safe_extract_all(archive: zipfile.ZipFile, target_dir: Path, max_bytes: int) -> None:
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    names: dict[str, bool] = {}
    explicit_names: set[str] = set()
    total = 0
    for member in archive.infolist():
        parts = _validate_member(member)
        name = "/".join(parts).casefold()
        if name in explicit_names or (name in names and names[name] != member.is_dir()):
            raise ValueError(f"Conflicting archive member: {member.filename}")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index]).casefold()
            if parent in names and not names[parent]:
                raise ValueError(f"Archive directory conflicts with a file: {member.filename}")
            names[parent] = True
        names[name] = member.is_dir()
        explicit_names.add(name)
        total += member.file_size
        if total > max_bytes:
            raise RuntimeError("publication exceeds temporary disk budget")
        members.append((member, parts))
    remaining = max_bytes
    for member, parts in members:
        destination = target_dir.joinpath(*parts)
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, destination.open("xb") as output:
            remaining -= _copy_limited(source, output, remaining)


class JwpubChecksumStore:
    """
    Persist JW API MD5 checksums alongside cached .jwpub files.

    Key   : "<pub>_<lang>_<issue>"  e.g. "mwb_T_202503"
    Value : MD5 hex string from the API.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._json = JsonFileRepository(path)
        self._json.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _CHECKSUM_STORE_LOCK

    def _load(self) -> dict[str, str]:
        try:
            if self._json.exists():
                data = self._json.read()
                if isinstance(data, dict):
                    return {str(key): str(value) for key, value in data.items()}
                log.warning(
                    "ChecksumStore: unexpected format in %s; resetting",
                    self._json.path,
                )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            log.warning("ChecksumStore: could not load %s: %s", self._json.path, exc)
        return {}

    def _flush(self, data: dict[str, str]) -> None:
        try:
            self._json.write(data, sort_keys=True)
        except OSError as exc:
            log.error("ChecksumStore: could not save %s: %s", self._json.path, exc)

    @staticmethod
    def _key(pub: str, lang: str, issue: str) -> str:
        return f"{pub}_{lang}_{issue}"

    def get(self, pub: str, lang: str, issue: str) -> str:
        with self._lock:
            return self._load().get(self._key(pub, lang, issue), "")

    def save(self, pub: str, lang: str, issue: str, checksum: str) -> None:
        if not checksum:
            return
        with self._lock:
            data = self._load()
            data[self._key(pub, lang, issue)] = checksum
            self._flush(data)

    def delete(self, pub: str, lang: str, issue: str) -> None:
        """Forget an evicted archive without resurrecting another store's entries."""
        with self._lock:
            data = self._load()
            if data.pop(self._key(pub, lang, issue), None) is not None:
                self._flush(data)

    def has_changed(
        self,
        pub: str,
        lang: str,
        issue: str,
        remote_checksum: str,
    ) -> bool:
        if not remote_checksum:
            return False
        stored = self.get(pub, lang, issue)
        changed = stored != remote_checksum
        if changed:
            log.info(
                "ChecksumStore: %s/%s/%s checksum changed (%s -> %s)",
                pub,
                lang,
                issue,
                stored or "<none>",
                remote_checksum,
            )
        return changed


def local_jwpub_checksum(path: Path) -> str:
    try:
        digest = hashlib.md5()  # nosec B324 - JW API exposes MD5 checksums
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def needs_jwpub_download(
    cache: JwpubCache,
    checksum_store: JwpubChecksumStore,
    pub: str,
    lang: str,
    issue: str,
    checksum: str,
) -> bool:
    """
    Return whether a JWPUB archive needs to be downloaded.

    Existing extracted data is trusted when the original archive/checksum is
    missing; this preserves old usable caches without redownloading forever.
    """
    has_extract = cache.is_cached(pub, lang, issue)
    archive = cache.jwpub_path(pub, lang, issue)
    has_archive = archive.is_file()

    if not has_extract and not has_archive:
        return True
    if not checksum:
        return False

    stored = checksum_store.get(pub, lang, issue)
    if stored == checksum:
        return False

    local_checksum = local_jwpub_checksum(archive) if has_archive else ""
    if local_checksum and local_checksum == checksum:
        checksum_store.save(pub, lang, issue, checksum)
        return False

    if not stored and has_extract and not has_archive:
        checksum_store.save(pub, lang, issue, checksum)
        return False

    return True


__all__ = [
    "JwpubCache",
    "JwpubChecksumStore",
    "local_jwpub_checksum",
    "needs_jwpub_download",
]
