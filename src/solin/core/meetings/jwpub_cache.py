from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import threading
import zipfile
from pathlib import Path

from solin.core.storage.json_repository import JsonFileRepository

log = logging.getLogger(__name__)


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
        return extract_path.exists() and any(extract_path.glob("*.db"))

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
        extract_path = self.extract_dir(pub, lang, issue)
        if not extract_path.exists():
            return
        try:
            shutil.rmtree(extract_path)
            log.debug("JwpubCache: invalidated extract dir %s", extract_path)
        except OSError as exc:
            log.error(
                "JwpubCache: could not remove extract dir %s: %s",
                extract_path,
                exc,
            )

    def extract(self, pub: str, lang: str, issue: str) -> Path | None:
        jwpub = self.jwpub_path(pub, lang, issue)
        if not jwpub.exists():
            return None
        extract_path = self.extract_dir(pub, lang, issue)
        extract_path.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(jwpub, "r") as outer:
                if "contents" not in outer.namelist():
                    return None
                inner_bytes = outer.read("contents")
            with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner:
                _safe_extract_all(inner, extract_path)
            return extract_path
        except (
            EOFError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            log.error("Extract failed %s: %s", jwpub, exc)
            return None


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
