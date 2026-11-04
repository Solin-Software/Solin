"""Local, cancellable JWPUB inspection and selected playlist materialization."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import weakref
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Collection
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from solin.core.jw.jwpub_catalog import JwpubCatalog, JwpubMedia, read_catalog
from solin.core.jw.publication_links import (
    PublicationMediaRequest,
    PublicationMediaResolver,
    PubMediaFile,
)
from solin.core.storage.archive_budget import ArchiveBudget

log = logging.getLogger(__name__)
_PUBLICATION_MEDIA_RESOLVER = PublicationMediaResolver()
_CHUNK_SIZE = 1024 * 1024
_PREVIEW_CACHE_BYTES = 32 * 1024 * 1024
_MANIFEST_MAX_BYTES = 1024 * 1024
_DESTINATION_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_DESTINATION_LOCKS_GUARD = threading.Lock()

PublicationMediaResolveFn: TypeAlias = Callable[
    [str, int | str | None, int | str | None, int | str | None, str],
    PubMediaFile | None,
]
Cancellation: TypeAlias = Callable[[], bool] | None


class JwpubImportCancelled(InterruptedError):
    """The owner requested cancellation of publication work."""


class JwpubSourceChangedError(ValueError):
    """The source is no longer the archive inspected by this session."""


@dataclass(frozen=True, slots=True)
class JwpubImportRequest:
    jwpub_path: str
    language: str = "T"
    dest_images_dir: str | None = None
    resolve_urls: bool = True


class JwpubPlaylistImportService:
    """Open publication sessions; whole imports use the same selection pipeline."""

    def __init__(self, resolve_media: PublicationMediaResolveFn | None = None) -> None:
        self._resolve_media = resolve_media or _resolve_publication_media

    def open(
        self,
        request: JwpubImportRequest,
        *,
        cancelled: Cancellation = None,
    ) -> JwpubPublication:
        return JwpubPublication(request, self._resolve_media, cancelled=cancelled)

    def read(self, request: JwpubImportRequest) -> tuple[list[dict], str]:
        with self.open(request) as publication:
            # Legacy callers without a destination own these durable local outputs.
            destination = request.dest_images_dir or tempfile.mkdtemp(prefix="solin_jwpub_media_")
            try:
                result = publication.materialize(
                    None,
                    dest_images_dir=destination,
                    resolve_urls=request.resolve_urls,
                )
            except BaseException:  # noqa: BLE001 - release owned output directory on every failure
                if not request.dest_images_dir:
                    shutil.rmtree(destination)
                raise
            if not request.dest_images_dir and not any(Path(destination).iterdir()):
                Path(destination).rmdir()
            return result


class JwpubPublication:
    """An immutable catalog backed by a private archive and database snapshot.

    Calls serialize through one lock, including close. Owners must cancel running
    work before closing; the session never removes successfully returned outputs.
    """

    def __init__(
        self,
        request: JwpubImportRequest,
        resolver: PublicationMediaResolveFn,
        *,
        cancelled: Cancellation = None,
    ) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._resolver = resolver
        self._source = Path(request.jwpub_path).expanduser().absolute()
        self._fingerprint = _fingerprint(self._source)
        self._source_digest = _hash_file(self._source, cancelled)
        self._stem = self._source.stem
        self._budget = ArchiveBudget()
        self._previews: OrderedDict[str, bytes] = OrderedDict()
        self._preview_bytes = 0
        self._resource_identities: dict[str, tuple[str, str]] = {}
        _check_cancelled(cancelled)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="solin_jwpub_", ignore_cleanup_errors=True
        )
        try:
            self._catalog = self._open(request.language, cancelled)
            self._media: dict[str, JwpubMedia] = {}
            for document in self._catalog.documents:
                for item in document.media:
                    self._media.setdefault(item.id, item)
            self.validate_source(cancelled=cancelled)
            _check_cancelled(cancelled)
        except BaseException:  # noqa: BLE001 - release partially opened archive snapshots
            self._temporary.cleanup()
            raise

    @property
    def catalog(self) -> JwpubCatalog:
        return self._catalog

    def _open(self, language: str, cancelled: Cancellation) -> JwpubCatalog:
        root = Path(self._temporary.name)
        self._contents = root / "contents.zip"
        manifest: dict = {}
        with zipfile.ZipFile(self._source) as outer:
            members = _members(outer, self._budget, cancelled)
            if "contents" not in members:
                raise ValueError("Not a valid .jwpub (no 'contents' entry)")
            with outer.open(members["contents"]) as source, self._contents.open("xb") as output:
                self._budget.copy_stream(
                    source, output, check_cancelled=lambda: _check_cancelled(cancelled)
                )
            if "manifest.json" in members:
                entry = members["manifest.json"]
                if entry.file_size > _MANIFEST_MAX_BYTES:
                    raise ValueError("Publication manifest is too large")
                with outer.open(entry) as source, io.BytesIO() as output:
                    self._budget.copy_stream(
                        source, output, check_cancelled=lambda: _check_cancelled(cancelled)
                    )
                    metadata = json.loads(output.getvalue())
                if not isinstance(metadata, dict) or not isinstance(
                    metadata.get("publication", {}), dict
                ):
                    raise ValueError("Invalid publication manifest")
                manifest = metadata.get("publication", {})

        database = root / "publication.db"
        with zipfile.ZipFile(self._contents) as inner:
            self._members = _members(inner, self._budget, cancelled)
            databases = [name for name in self._members if name.lower().endswith(".db")]
            requested_db = manifest.get("fileName")
            if requested_db in databases:
                db_name = requested_db
            elif len(databases) == 1:
                db_name = databases[0]
            else:
                raise ValueError("Publication must identify one SQLite database")
            with inner.open(self._members[db_name]) as source, database.open("xb") as output:
                self._budget.copy_stream(
                    source, output, check_cancelled=lambda: _check_cancelled(cancelled)
                )
        _check_cancelled(cancelled)
        return read_catalog(
            database,
            manifest=manifest,
            source_path=str(self._source),
            language=language,
            members=set(self._members),
            check_cancelled=lambda: _check_cancelled(cancelled),
        )

    def validate_source(self, *, cancelled: Cancellation = None) -> None:
        """Verify identity and content, including changes that preserve timestamps.

        This performs incremental file I/O and belongs in an import worker.
        """
        with self._lock:
            self._validate_source_metadata()
            try:
                digest = _hash_file(self._source, cancelled)
            except JwpubImportCancelled:
                raise
            except OSError as exc:
                raise JwpubSourceChangedError("Publication source is no longer accessible") from exc
            if digest != self._source_digest:
                raise JwpubSourceChangedError("Publication source content changed; open it again")
            self._validate_source_metadata()

    def _validate_source_metadata(self) -> None:
        self._ensure_open()
        try:
            current = _fingerprint(self._source)
        except (OSError, ValueError) as exc:
            raise JwpubSourceChangedError("Publication source is no longer accessible") from exc
        if current != self._fingerprint:
            raise JwpubSourceChangedError("Publication source changed; open it again")

    def preview(self, media_id: str, *, cancelled: Cancellation = None) -> bytes | None:
        """Read a bounded local image preview. Never resolve or download media."""
        with self._lock:
            self._validate_source_metadata()
            _check_cancelled(cancelled)
            item = self._media[media_id]
            path = item.preview_member_path
            if not path:
                return None
            cached = self._previews.get(path)
            if cached is not None:
                self._previews.move_to_end(path)
                return cached
            member = self._members[path]
            if member.file_size > _PREVIEW_CACHE_BYTES:
                return None
            with (
                zipfile.ZipFile(self._contents) as inner,
                inner.open(member) as source,
                io.BytesIO() as output,
            ):
                self._budget.copy_stream(
                    source, output, check_cancelled=lambda: _check_cancelled(cancelled)
                )
                data = output.getvalue()
            self._validate_source_metadata()
            _check_cancelled(cancelled)
            if not _is_image(data):
                return None
            while self._previews and self._preview_bytes + len(data) > _PREVIEW_CACHE_BYTES:
                _, evicted = self._previews.popitem(last=False)
                self._preview_bytes -= len(evicted)
            self._previews[path] = data
            self._preview_bytes += len(data)
            return data

    def materialize(
        self,
        selected_ids: Collection[str] | None,
        *,
        dest_images_dir: str,
        resolve_urls: bool = True,
        cancelled: Cancellation = None,
        created_paths: list[str] | None = None,
    ) -> tuple[list[dict], str]:
        """Import unique selected IDs in catalog order and atomically own new files.

        ``None`` selects all media. Unknown IDs are rejected. On success,
        ``created_paths`` receives only new durable outputs; on any failure they
        are removed and the accumulator stays unchanged. Reused files survive.
        Owners that may roll back a successful batch later must use a private
        destination until insertion commits; the accumulator does not lease files.
        """
        with self._lock, _destination_guard(dest_images_dir, cancelled):
            self.validate_source(cancelled=cancelled)
            _check_cancelled(cancelled)
            selected = set(self._media) if selected_ids is None else set(selected_ids)
            if selected - self._media.keys():
                raise ValueError("Selection contains media outside this publication")
            created: list[Path] = []
            resources: dict[str, str] = {}
            items: list[dict] = []
            try:
                with zipfile.ZipFile(self._contents) as inner:
                    for media_id, media in self._media.items():
                        _check_cancelled(cancelled)
                        if media_id not in selected:
                            continue
                        item = self._item(media, resolve_urls, cancelled)
                        if media.member_path:
                            if media.member_path not in resources:
                                resources[media.member_path] = self._persist(
                                    inner,
                                    media,
                                    Path(dest_images_dir),
                                    created,
                                    cancelled,
                                )
                            item["url"] = resources[media.member_path]
                        items.append(item)
                self.validate_source(cancelled=cancelled)
                _check_cancelled(cancelled)
            except BaseException:  # noqa: BLE001 - rollback owned outputs and preserve the failure
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                raise
            if created_paths is not None:
                created_paths.extend(str(path) for path in created)
            return items, self._stem

    def _item(self, media: JwpubMedia, resolve_urls: bool, cancelled: Cancellation) -> dict:
        title, url = media.title, ""
        if resolve_urls and not media.member_path and media.media_type != "image":
            try:
                resolved = self._resolver(
                    media.key_symbol,
                    media.track,
                    media.issue_tag,
                    media.doc_id,
                    media.language,
                )
                if resolved is not None:
                    title, url = resolved.title or title, resolved.url
            except (JwpubImportCancelled, InterruptedError):
                raise
            except Exception:  # noqa: BLE001 - isolate individual external resolver failures
                log.warning("Publication media resolution failed for %s", media.id, exc_info=True)
            _check_cancelled(cancelled)
        return {
            "source_item_id": media.id,
            "title": title,
            "url": url,
            "type": media.media_type,
            "key_symbol": media.key_symbol or None,
            "track": media.track or None,
            "issue_tag": media.issue_tag or None,
            "doc_id": media.doc_id or None,
            "meps_language": media.meps_language,
        }

    def _persist(
        self,
        archive: zipfile.ZipFile,
        media: JwpubMedia,
        destination: Path,
        created: list[Path],
        cancelled: Cancellation,
    ) -> str:
        destination.mkdir(parents=True, exist_ok=True)
        identity = self._resource_identities.get(media.member_path)
        if identity is not None:
            digest, suffix = identity
            path = destination / f"{digest}{suffix}"
            if path.exists() or path.is_symlink():
                self._verify_resource(path, digest, cancelled)
                return str(path)
        # A staging file in the destination allows atomic publication with a
        # no-overwrite hard link, and never exposes partial files under stable IDs.
        staging: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".jwpub-", suffix=".part", dir=destination, delete=False
            ) as output:
                staging = Path(output.name)
                with archive.open(self._members[media.member_path]) as source:
                    self._budget.copy_stream(
                        source, output, check_cancelled=lambda: _check_cancelled(cancelled)
                    )
            digest = _hash_file(staging, cancelled)
            suffix = _media_suffix(media)
            self._resource_identities[media.member_path] = digest, suffix
            path = destination / f"{digest}{suffix}"
            _check_cancelled(cancelled)
            try:
                os.link(staging, path)
            except FileExistsError:
                self._verify_resource(path, digest, cancelled)
            except OSError:
                # Filesystems without hard-link support still use exclusive
                # creation and incremental copying; never replace shared assets.
                try:
                    output = path.open("xb")
                except FileExistsError:
                    self._verify_resource(path, digest, cancelled)
                else:
                    created.append(path)
                    with output, staging.open("rb") as source:
                        while chunk := source.read(_CHUNK_SIZE):
                            _check_cancelled(cancelled)
                            output.write(chunk)
            else:
                created.append(path)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return str(path)

    def _verify_resource(self, path: Path, digest: str, cancelled: Cancellation) -> None:
        _check_cancelled(cancelled)
        if path.is_symlink():
            raise ValueError("Stored publication resource is a symbolic link")
        _fingerprint(path)
        if _hash_file(path, cancelled) != digest:
            raise ValueError("Stored publication resource conflicts with its content identity")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Publication session is closed")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._previews.clear()
                self._preview_bytes = 0
                self._resource_identities.clear()
                self._temporary.cleanup()

    def __enter__(self) -> JwpubPublication:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _check_cancelled(cancelled: Cancellation) -> None:
    if cancelled is not None and cancelled():
        raise JwpubImportCancelled("Publication import cancelled")


@contextmanager
def _destination_guard(destination: str, cancelled: Cancellation):
    """Keep other sessions from adopting files before a tentative batch commits."""
    key = os.path.normcase(os.path.realpath(destination))
    with _DESTINATION_LOCKS_GUARD:
        lock = _DESTINATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DESTINATION_LOCKS[key] = lock
    while not lock.acquire(timeout=0.05):
        _check_cancelled(cancelled)
    try:
        _check_cancelled(cancelled)
        yield
    finally:
        lock.release()


def _fingerprint(path: Path) -> tuple[int, ...]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Publication source is not a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _members(
    archive: zipfile.ZipFile,
    budget: ArchiveBudget,
    cancelled: Cancellation,
) -> dict[str, zipfile.ZipInfo]:
    budget.check_members(archive.infolist())
    result: dict[str, zipfile.ZipInfo] = {}
    names: dict[str, bool] = {}
    explicit: set[str] = set()
    for member in archive.infolist():
        _check_cancelled(cancelled)
        path = member.filename.removesuffix("/") if member.is_dir() else member.filename
        parts = path.split("/")
        if member.orig_filename != member.filename:
            raise ValueError("Archive member name was normalized or contains NUL")
        for part in parts:
            stem = part.split(".", 1)[0].upper()
            reserved = stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            reserved = reserved or (
                len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"
            )
            if (
                not part
                or part in {".", ".."}
                or part.endswith((".", " "))
                or reserved
                or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in part)
            ):
                raise ValueError(f"Unsafe archive member: {member.filename!r}")
        mode = stat.S_IFMT(member.external_attr >> 16)
        if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.external_attr & 0x400:
            raise ValueError("Archive contains a symbolic link or special file")
        if (mode == stat.S_IFDIR and not member.is_dir()) or (
            member.is_dir() and (mode == stat.S_IFREG or member.file_size != 0)
        ):
            raise ValueError("Archive contains an inconsistent directory")
        name = path.casefold()
        if name in explicit or (name in names and names[name] != member.is_dir()):
            raise ValueError("Archive contains conflicting member names")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index]).casefold()
            if parent in names and not names[parent]:
                raise ValueError("Archive file conflicts with a directory")
            names[parent] = True
        explicit.add(name)
        names[name] = member.is_dir()
        if not member.is_dir():
            result[member.filename] = member
    return result


def _hash_file(path: Path, cancelled: Cancellation) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            _check_cancelled(cancelled)
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _media_suffix(media: JwpubMedia) -> str:
    suffix = Path(media.member_path).suffix.lower()
    suffix = {".jpeg": ".jpg", ".tif": ".tiff"}.get(suffix, suffix)
    if 1 < len(suffix) <= 20 and suffix[1:].isascii() and suffix[1:].isalnum():
        return suffix
    return {"image": ".jpg", "video": ".mp4", "audio": ".mp3"}[media.media_type]


def _is_image(data: bytes) -> bool:
    return data.startswith(
        (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")
    ) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def _resolve_publication_media(
    key_symbol: str,
    track: int | str | None,
    issue_tag: int | str | None,
    meps_doc_id: int | str | None,
    language: str,
) -> PubMediaFile | None:
    return _PUBLICATION_MEDIA_RESOLVER.resolve_media(
        PublicationMediaRequest(
            key_symbol=key_symbol,
            track=int(track or 0),
            issue_tag=int(issue_tag or 0),
            meps_doc_id=int(meps_doc_id or 0),
            language=language,
        )
    )


def read_jwpub_playlist_items(
    request: JwpubImportRequest,
    *,
    resolve_media: PublicationMediaResolveFn | None = None,
) -> tuple[list[dict], str]:
    """Import the full publication through the shared catalog session."""
    return JwpubPlaylistImportService(resolve_media).read(request)


__all__ = [
    "JwpubImportRequest",
    "JwpubImportCancelled",
    "JwpubSourceChangedError",
    "JwpubPlaylistImportService",
    "JwpubPublication",
    "PublicationMediaResolveFn",
    "read_jwpub_playlist_items",
]
