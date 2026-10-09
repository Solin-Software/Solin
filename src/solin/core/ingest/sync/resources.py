"""Portable resource identity, cached content hashes, and recoverable retirement.

Filesystem revisions are strictly local cache keys. Only size and SHA256 leave
this module. Retirement must follow a durable logical deletion in the journal.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection, Mapping
import ctypes
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
from threading import Lock
import unicodedata
from uuid import NAMESPACE_URL, uuid4, uuid5

from solin.core.ingest.staging import (
    WATCHED_FOLDER_STAGING_SUFFIX,
    is_watched_folder_staging_path,
)
from solin.core.media.local_source import local_source_revision_from_stat


_CACHE_LIMIT = 32768
_SIGNATURES: OrderedDict[str, tuple[tuple[int, ...], dict[str, str | int]]] = OrderedDict()
_DIRECTORY_NAMES: OrderedDict[Path, tuple[tuple[int, ...], dict[str, tuple[Path, ...]]]] = (
    OrderedDict()
)
_CACHE_LOCK = Lock()
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"
}


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("attributes", ctypes.c_uint32),
    ]


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def resource_identity_key(key: str) -> str:
    """Normalize local identity without changing the usable file-path spelling."""
    return (
        key
        if key.lower().startswith(("http://", "https://"))
        else _name_key(key.replace("\\", "/"))
    )


def _validate_parts(parts: list[str]) -> None:
    if not parts or any(
        not part
        or part in {".", ".."}
        or part[-1] in ". "
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        or part.split(".", 1)[0].casefold() in _RESERVED_NAMES
        or _name_key(part) == ".solin_sync"
        or is_watched_folder_staging_path(part)
        for part in parts
    ):
        raise ValueError("Resource path is not portable")


def _directory_names(directory: Path) -> dict[str, tuple[Path, ...]]:
    """Index names once per directory revision, avoiding quadratic tree scans."""
    revision = tuple(local_source_revision_from_stat(directory.stat()))
    with _CACHE_LOCK:
        cached = _DIRECTORY_NAMES.get(directory)
        if cached is not None and cached[0] == revision:
            _DIRECTORY_NAMES.move_to_end(directory)
            return cached[1]
    entries: dict[str, list[Path]] = {}
    for entry in directory.iterdir():
        entries.setdefault(_name_key(entry.name), []).append(entry)
    result = {key: tuple(paths) for key, paths in entries.items()}
    if revision != tuple(local_source_revision_from_stat(directory.stat())):
        raise OSError("Linked folder changed while validating resource names")
    with _CACHE_LOCK:
        _DIRECTORY_NAMES[directory] = revision, result
        _DIRECTORY_NAMES.move_to_end(directory)
        while len(_DIRECTORY_NAMES) > 128:
            _DIRECTORY_NAMES.popitem(last=False)
    return result


def _contained_path(folder: Path, key: str) -> Path:
    """Resolve spelling locally, rejecting ambiguous names and escaping links."""
    root = folder.resolve()
    parts = key.replace("\\", "/").split("/")
    _validate_parts(parts)
    current = root
    for part in parts:
        if current.is_dir():
            matches = _directory_names(current).get(_name_key(part), ())
            if len(matches) > 1:
                raise ValueError("Resource names collide across platforms")
            current = matches[0] if matches else current / part
        else:
            current = current / part
        if not current.resolve().is_relative_to(root):
            raise ValueError("Resource path escapes its linked folder")
    return current


def portable_resource_key(url: str, folder: Path) -> str:
    """Return an NFC relative POSIX path, or an unchanged HTTP(S) URL.

    Absolute local paths must belong to this machine's linked folder. Relative
    paths from another machine are accepted only when portable and unambiguous.
    """
    if url.lower().startswith(("https://", "http://")):
        return url
    root = folder.resolve()
    value = url.replace("\\", "/")
    if ".." in value.split("/"):
        raise ValueError("Resource path must not contain traversal")
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            value = candidate.resolve().relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError("Resource path escapes its linked folder") from error
    elif PureWindowsPath(value).drive or value.startswith("/"):
        raise ValueError("Foreign absolute resource path is not portable")
    parts = value.split("/")
    _validate_parts(parts)
    key = unicodedata.normalize("NFC", "/".join(parts))
    _contained_path(root, key)
    return key


def content_identity(value: object) -> tuple[int, str] | None:
    """Validate the portable size/digest pair; filesystem metadata is not identity."""
    if not isinstance(value, Mapping):
        return None
    size, digest = value.get("size"), value.get("sha256")
    if (
        not isinstance(size, int) or isinstance(size, bool) or size < 0
        or not isinstance(digest, str) or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        return None
    return size, digest


def automatic_occurrence_id(resource_key: str, *, content: object = None) -> str:
    """Give replicas the same fallback identity for a single document resource."""
    key = resource_identity_key(resource_key)
    version = content_identity(content)
    suffix = f":{version[0]}:{version[1]}" if version is not None else ""
    return str(uuid5(NAMESPACE_URL, f"solin:linked-folder:automatic:{key}{suffix}"))


def _revision(descriptor: int) -> tuple[int, ...]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Content signatures require a regular file")
    revision = tuple(local_source_revision_from_stat(info))
    if os.name != "nt":
        return revision
    # Windows st_ctime is creation time. ChangeTime detects an in-place write
    # even when an editor restores both the file size and modification time.
    import msvcrt

    basic = _WindowsFileBasicInfo()
    query = ctypes.windll.kernel32.GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    if not query(
        ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
        0,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        raise ctypes.WinError()
    # NTFS ChangeTime may be identical for rapid writes within one clock tick.
    # Its per-file USN changes on each closed write even in that case.
    control = ctypes.windll.kernel32.DeviceIoControl
    control.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    control.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    returned = ctypes.c_uint32()
    success = control(
        ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
        0x000900EB,
        None,
        0,
        buffer,
        len(buffer),
        ctypes.byref(returned),
        None,
    )
    if success and returned.value >= 32 and int.from_bytes(buffer.raw[4:6], "little") == 2:
        return (*revision, basic.change_time, int.from_bytes(buffer.raw[24:32], "little"))
    return (*revision, basic.change_time)


def content_signature(path: Path) -> dict[str, str | int]:
    """Hash content once per local revision; raise if it changes while reading."""
    # The descriptor validates content identity. Resolving every ancestor here
    # adds filesystem calls but provides no extra correctness to this cache.
    cache_key = os.path.normcase(os.path.abspath(path))
    with path.open("rb") as source:
        before = _revision(source.fileno())
        source_size = before[0]
        # Windows metadata is not a portable content-generation counter. NTFS
        # USNs normally advance on writes, but other Windows runner/storage
        # configurations can report the same USN after an in-place rewrite
        # whose size and mtime are restored. Reusing that digest would publish
        # a stale content identity, so correctness takes precedence over this
        # local hash cache on Windows.
        cacheable = os.name != "nt"
        with _CACHE_LOCK:
            cached = _SIGNATURES.get(cache_key)
            if cacheable and cached is not None and cached[0] == before:
                _SIGNATURES.move_to_end(cache_key)
                return dict(cached[1])
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        if before != _revision(source.fileno()):
            raise OSError("Resource changed while calculating its content signature")
    result: dict[str, str | int] = {"sha256": digest, "size": source_size}
    with _CACHE_LOCK:
        _SIGNATURES[cache_key] = before, result
        _SIGNATURES.move_to_end(cache_key)
        while len(_SIGNATURES) > _CACHE_LIMIT:
            _SIGNATURES.popitem(last=False)
    return dict(result)


def _archive_directory(folder: Path, key: str, document_id: str | None) -> Path:
    root = folder.resolve()
    digest = hashlib.sha256(_name_key(key).encode("utf-8")).hexdigest()
    directory = root / ".solin_sync" / "resources"
    if document_id is not None:
        if len(document_id) != 64 or any(char not in "0123456789abcdef" for char in document_id):
            raise ValueError("Invalid document identity for resource archive")
        directory /= document_id
    directory /= digest
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Resource archive escapes its linked folder")
    return directory


def _archive_child(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Archived resource must be a contained regular file")
    return path


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_durable(source: Path, destination: Path, *, replace: bool = True) -> None:
    """Publish a fully flushed copy; never expose partial media to the scanner."""
    staging = destination.with_name(
        f".{destination.name}.{uuid4().hex}{WATCHED_FOLDER_STAGING_SUFFIX}"
    )
    try:
        with source.open("rb") as reader, staging.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 4 * 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, staging)
        if replace:
            os.replace(staging, destination)
        elif os.name == "nt":
            # Windows rename fails if another writer created the destination,
            # and also works on volumes without hard-link support.
            os.rename(staging, destination)
        else:
            # Linking publishes without replacing a concurrent writer's file.
            # Both paths are in the same directory and filesystem.
            os.link(staging, destination)
        _sync_directory(destination.parent)
    finally:
        staging.unlink(missing_ok=True)


def _archive_resource(
    source: Path, folder: Path, key: str, document_id: str | None,
    signature: dict[str, str | int],
) -> None:
    directory = _archive_directory(folder, key, document_id)
    directory.mkdir(parents=True, exist_ok=True)
    archived = _archive_child(directory, str(signature["sha256"]))
    if not archived.exists():
        _copy_durable(source, archived)
    if content_signature(archived) != signature or content_signature(source) != signature:
        raise OSError("Resource changed during retirement")
    metadata = _archive_child(directory, f"{signature['sha256']}.json")
    if not metadata.exists():
        staging = metadata.with_name(
            f".{metadata.name}.{uuid4().hex}{WATCHED_FOLDER_STAGING_SUFFIX}"
        )
        try:
            with staging.open("x", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "resource": key, **signature},
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, metadata)
        finally:
            staging.unlink(missing_ok=True)
    # Persist both the archive entry and any newly created directory ancestors
    # before removal makes the archived content the only remaining local copy.
    current = directory
    root = folder.resolve()
    while True:
        _sync_directory(current)
        if current == root:
            break
        current = current.parent


def _finish_retirement(
    candidate: Path, destination: Path, folder: Path, key: str, document_id: str | None,
    expected_contents: Collection[tuple[int, str]],
) -> bool:
    """Validate the atomically captured file, preserving any concurrent replacement."""
    candidate = _archive_child(candidate.parent, candidate.name)
    signature = content_signature(candidate)
    _archive_resource(candidate, folder, key, document_id, signature)
    removed = content_identity(signature) in expected_contents
    if not removed:
        try:
            _copy_durable(candidate, destination, replace=False)
        except FileExistsError:
            pass  # Both replacements survive: visible destination and verified archive.
    if content_signature(candidate) != signature:
        raise OSError("Resource changed during retirement")
    candidate.unlink()
    _sync_directory(candidate.parent)
    _sync_directory(destination.parent)
    return removed


def retire_file(
    folder: Path, relative_key: str, *, document_id: str | None = None,
    expected_contents: Collection[tuple[int, str]] | None = None,
) -> bool:
    """Archive before removal; conditional cleanup owns bytes, never a reused path.

    Captured retirement files survive interruption and are resumed on retry.
    Different captured content is restored without overwriting a concurrent file.
    """
    key = portable_resource_key(relative_key, folder)
    if key.lower().startswith(("https://", "http://")):
        return False
    source = _contained_path(folder, key)
    directory = _archive_directory(folder, key, document_id)
    removed = False
    if expected_contents is not None and directory.is_dir():
        for candidate in directory.glob(f".retiring-*{WATCHED_FOLDER_STAGING_SUFFIX}"):
            candidate = _archive_child(directory, candidate.name)
            removed = _finish_retirement(
                candidate, source, folder, key, document_id, expected_contents,
            ) or removed
    if not source.exists():
        return removed
    signature = content_signature(source)
    if expected_contents is not None and content_identity(signature) not in expected_contents:
        return removed
    _archive_resource(source, folder, key, document_id, signature)
    if expected_contents is None:
        if content_signature(source) != signature:
            raise OSError("Resource changed during retirement")
        source.unlink()
        _sync_directory(source.parent)
        return True
    candidate = _archive_child(directory, f".retiring-{uuid4().hex}{WATCHED_FOLDER_STAGING_SUFFIX}")
    try:
        source.rename(candidate)
    except FileNotFoundError:
        return removed
    _sync_directory(directory)
    _sync_directory(source.parent)
    return _finish_retirement(candidate, source, folder, key, document_id, expected_contents) or removed


def _archived_versions(
    folder: Path, key: str, document_id: str | None, *, require_complete: bool = False,
) -> dict[tuple[int, str], Path]:
    directory = _archive_directory(folder, key, document_id)
    if not directory.is_dir():
        return {}
    if require_complete and any(directory.glob(f".retiring-*{WATCHED_FOLDER_STAGING_SUFFIX}")):
        raise OSError("Resource retirement has not completed")
    versions: dict[tuple[int, str], Path] = {}
    for metadata in directory.glob("*.json"):
        metadata = _archive_child(directory, metadata.name)
        try:
            record = json.loads(metadata.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as error:
            raise OSError("Incomplete resource archive metadata") from error
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or _name_key(str(record.get("resource", ""))) != _name_key(key)
        ):
            raise OSError("Invalid resource archive metadata")
        signature = content_identity(record)
        if signature is None:
            raise OSError("Invalid archived resource digest")
        digest = signature[1]
        archived = _archive_child(directory, digest)
        if not archived.exists():
            if require_complete:
                raise OSError("Archived resource content has not arrived")
            continue  # Cloud transport may deliver the metadata first.
        if content_signature(archived) != {"size": record.get("size"), "sha256": digest}:
            raise OSError("Archived resource content does not match metadata")
        versions[signature] = archived
    return versions


def archived_content_signatures(
    folder: Path, relative_key: str, *, document_id: str | None,
) -> list[dict[str, str | int]]:
    """Identify legacy deleted content only from complete, verified recovery copies."""
    key = portable_resource_key(relative_key, folder)
    versions = _archived_versions(folder, key, document_id, require_complete=True)
    return [{"size": size, "sha256": digest} for size, digest in sorted(versions)]


def recover_file(folder: Path, relative_key: str, *, document_id: str | None = None) -> bool:
    """Recover one unambiguous archived version without replacing visible data.

    Multiple different archived versions require explicit conflict resolution;
    selecting by machine timestamps could silently restore the wrong content.
    """
    key = portable_resource_key(relative_key, folder)
    if key.lower().startswith(("https://", "http://")):
        return False
    destination = _contained_path(folder, key)
    if destination.exists():
        return destination.is_file()
    directory = _archive_directory(folder, key, document_id)
    if directory.is_dir():
        for candidate in directory.glob(f".retiring-*{WATCHED_FOLDER_STAGING_SUFFIX}"):
            _finish_retirement(
                _archive_child(directory, candidate.name), destination, folder, key, document_id, set(),
            )
        if destination.exists():
            return destination.is_file()
    versions = _archived_versions(folder, key, document_id)
    if not versions:
        return False
    if len(versions) > 1:
        raise OSError("Multiple archived resource versions require conflict resolution")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_durable(next(iter(versions.values())), destination, replace=False)
    except FileExistsError:
        return destination.is_file()
    return True
