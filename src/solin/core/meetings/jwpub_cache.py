from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path

from solin.core.storage.json_repository import JsonFileRepository

log = logging.getLogger(__name__)

_EXTRACTION_MARKER = ".solin-extraction-complete"
_EXTRACTION_STALE_MARKER = ".solin-extraction-stale"


class JwpubCache:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def jwpub_path(self, pub: str, lang: str, issue: str) -> Path:
        directory = self._root / f"{pub}_{lang}"
        directory.mkdir(exist_ok=True)
        return directory / f"{pub}_{lang}_{issue}.jwpub"

    def extract_dir(self, pub: str, lang: str, issue: str) -> Path:
        return self._root / f"{pub}_{lang}" / f"x_{issue}"

    def is_cached(self, pub: str, lang: str, issue: str) -> bool:
        extract_path = self.extract_dir(pub, lang, issue)
        if not extract_path.exists() or not any(extract_path.glob("*.db")):
            return False
        if (extract_path / _EXTRACTION_STALE_MARKER).is_file():
            return False
        return (
            (extract_path / _EXTRACTION_MARKER).is_file()
            or not self.jwpub_path(pub, lang, issue).is_file()
        )

    def can_materialize(self, pub: str, lang: str, issue: str) -> bool:
        """Return whether a publication can be rebuilt without a download."""

        return self.is_cached(pub, lang, issue) or self.jwpub_path(pub, lang, issue).is_file()

    def db_path(self, pub: str, lang: str, issue: str) -> Path | None:
        extract_path = self.extract_dir(pub, lang, issue)
        dbs = list(extract_path.glob("*.db"))
        return dbs[0] if dbs else None

    def invalidate_extract(self, pub: str, lang: str, issue: str) -> None:
        """
        Remove an extracted publication so the next read unpacks fresh contents.

        A missing directory is a no-op. Filesystem errors are logged and kept
        local because cache invalidation must never crash the meeting workflow.
        """
        self._remove_extract_dir(self.extract_dir(pub, lang, issue))

    def mark_extract_stale(self, pub: str, lang: str, issue: str) -> None:
        """Require a fresh extraction without removing files currently in use."""

        extract_path = self.extract_dir(pub, lang, issue)
        if not extract_path.is_dir():
            return
        try:
            (extract_path / _EXTRACTION_STALE_MARKER).touch(exist_ok=True)
        except OSError as exc:
            log.warning("JwpubCache: could not mark extract stale %s: %s", extract_path, exc)

    def repair_extract_for_source(self, source: str | os.PathLike[str]) -> bool:
        """Restore missing files from the owning archive without removing live files."""

        return self.repair_extracts_for_sources((source,)) > 0

    def repair_extracts_for_sources(
        self,
        sources: Iterable[str | os.PathLike[str]],
    ) -> int:
        """Repair each affected extraction once and return the successful count."""

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
                if staged.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, destination)
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
        return extract_path, publication_dir / f"{publication_dir.name}_{issue}.jwpub"

    def _mark_path_stale(self, extract_path: Path) -> None:
        if not extract_path.is_dir():
            return
        try:
            (extract_path / _EXTRACTION_STALE_MARKER).touch(exist_ok=True)
        except OSError as exc:
            log.warning("JwpubCache: could not mark extract stale %s: %s", extract_path, exc)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return

    def _remove_extract_dir(self, extract_path: Path) -> bool:
        if not extract_path.exists():
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

    def extract(self, pub: str, lang: str, issue: str) -> Path | None:
        jwpub = self.jwpub_path(pub, lang, issue)
        if not jwpub.exists():
            return None
        extract_path = self.extract_dir(pub, lang, issue)
        extract_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = self._stage_extract(jwpub, extract_path)
        if staging_path is None:
            return None
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
            if (
                previous_path is not None
                and previous_path.exists()
                and extract_path.exists()
            ):
                self._cleanup_tree(previous_path, "previous extract dir")

    def _stage_extract(self, jwpub: Path, extract_path: Path) -> Path | None:
        staging_path = Path(
            tempfile.mkdtemp(prefix=f".{extract_path.name}.", dir=extract_path.parent)
        )
        try:
            with zipfile.ZipFile(jwpub, "r") as outer:
                if "contents" not in outer.namelist():
                    raise RuntimeError("publication archive has no contents payload")
                inner_bytes = outer.read("contents")
            with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner:
                _safe_extract_all(inner, staging_path)
            if not any(staging_path.glob("*.db")):
                raise RuntimeError("publication has no database")
            (staging_path / _EXTRACTION_MARKER).touch(exist_ok=False)
            return staging_path
        except (
            EOFError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            log.error("Extract failed %s: %s", jwpub, exc)
            self._cleanup_tree(staging_path, "failed extraction staging dir")
            return None

    def _cleanup_tree(self, path: Path, label: str) -> None:
        try:
            self._remove_tree(path)
        except OSError as exc:
            log.warning("JwpubCache: could not clean %s %s: %s", label, path, exc)


def _safe_extract_all(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (target_dir / member.filename).resolve()
        if member_path != target_root and target_root not in member_path.parents:
            raise RuntimeError(f"Archive member escapes extraction dir: {member.filename}")
    archive.extractall(target_dir)


class JwpubChecksumStore:
    """
    Persist JW API MD5 checksums alongside cached .jwpub files.

    Key   : "<pub>_<lang>_<issue>"  e.g. "mwb_T_202503"
    Value : MD5 hex string from the API.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._json = JsonFileRepository(path)
        self._json.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, str] = self._load()

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

    def _flush(self) -> None:
        try:
            on_disk: dict[str, str] = {}
            try:
                if self._json.exists():
                    raw = self._json.read()
                    if isinstance(raw, dict):
                        on_disk = {
                            str(key): str(value)
                            for key, value in raw.items()
                        }
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                log.warning("ChecksumStore: re-read before flush failed: %s", exc)

            merged = {**on_disk, **self._data}
            self._data = merged
            self._json.write(merged, sort_keys=True)
        except OSError as exc:
            log.error("ChecksumStore: could not save %s: %s", self._json.path, exc)

    @staticmethod
    def _key(pub: str, lang: str, issue: str) -> str:
        return f"{pub}_{lang}_{issue}"

    def get(self, pub: str, lang: str, issue: str) -> str:
        with self._lock:
            return self._data.get(self._key(pub, lang, issue), "")

    def save(self, pub: str, lang: str, issue: str, checksum: str) -> None:
        if not checksum:
            return
        with self._lock:
            self._data[self._key(pub, lang, issue)] = checksum
            self._flush()

    def has_changed(
        self,
        pub: str,
        lang: str,
        issue: str,
        remote_checksum: str,
    ) -> bool:
        if not remote_checksum:
            return False
        with self._lock:
            stored = self._data.get(self._key(pub, lang, issue), "")
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
