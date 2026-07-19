"""Streaming reader/writer for Solin's native ``.solinplaylist`` package."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from typing import Any
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

from solin.core.jw.identifiers import is_jw_url
from solin.core.media.download_storage import completed_cached_path
from solin.core.media.formats import MEDIA_EXTS, mime_to_ext
from solin.core.media.jw_reference import parse_jw_media_reference
from solin.core.playlists.native_manifest import (
    portable_playlist_snapshot,
    regenerate_playlist_ids,
    validate_native_manifest,
)
from solin.core.playlists.native_types import (
    CancellationProbe,
    NativePlaylistError,
    NativePlaylistExportRequest,
    NativePlaylistExportResult,
    NativePlaylistImportRequest,
    NativePlaylistImportResult,
    NativePlaylistMissingMediaError,
    NativePlaylistProgress,
    NativePlaylistTransferCancelled,
    NativePlaylistValidationError,
    SOLIN_PLAYLIST_FORMAT,
    SOLIN_PLAYLIST_MANIFEST_MAX_BYTES,
    SOLIN_PLAYLIST_MAX_ENTRIES,
    SOLIN_PLAYLIST_MIME,
    SOLIN_PLAYLIST_SCHEMA_VERSION,
)


_CHUNK_SIZE = 1024 * 1024
_MIN_FREE_BYTES = 64 * 1024 * 1024
_STAGING_DIR_NAME = ".solinplaylist-staging"
_SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_MIMETYPE_ENTRY = "mimetype"
_MANIFEST_ENTRY = "manifest.json"


@dataclass(slots=True)
class _AssetPlan:
    identifier: str
    archive_path: str
    role: str
    item_ids: list[str]
    source_path: Path
    original_filename: str
    mime_type: str
    size: int
    signature: tuple[int, int, int]
    sha256: str = ""

    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "path": self.archive_path,
            "role": self.role,
            "item_ids": list(self.item_ids),
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(slots=True)
class _ExportPlan:
    playlist: dict[str, Any]
    sources: list[dict[str, Any]]
    assets: list[_AssetPlan]
    embedded_media: int
    thumbnails: int
    total_bytes: int


@dataclass(slots=True)
class _ImportAsset:
    descriptor: dict[str, Any]
    info: zipfile.ZipInfo
    stage_path: Path
    destination: Path | None = None


def export_native_playlist(
    request: NativePlaylistExportRequest,
) -> NativePlaylistExportResult:
    """Export a complete playlist snapshot through an atomic sibling file."""
    _check_cancelled(request.cancellation)
    _emit(request, "Preparing playlist", detail=str(request.playlist.get("name") or ""))
    plan = _build_export_plan(request)
    _check_cancelled(request.cancellation)

    output_path = Path(request.output_path)
    output_identity = os.path.normcase(os.fspath(output_path.resolve()))
    if any(
        os.path.normcase(os.fspath(asset.source_path.resolve())) == output_identity
        for asset in plan.assets
    ):
        raise NativePlaylistError("Export destination cannot overwrite playlist media")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".part",
        dir=output_path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    completed_bytes = 0
    try:
        with temp_path.open("w+b") as raw:
            with zipfile.ZipFile(raw, "w", allowZip64=True) as archive:
                _write_mimetype(archive)
                for index, asset in enumerate(plan.assets):
                    _check_cancelled(request.cancellation)
                    _emit(
                        request,
                        "Embedding media",
                        detail=asset.original_filename,
                        completed=completed_bytes,
                        total=plan.total_bytes,
                        items_completed=index,
                        items_total=len(plan.assets),
                        bytes_completed=completed_bytes,
                        bytes_total=plan.total_bytes,
                    )
                    written = _write_asset_streaming(
                        archive,
                        asset,
                        request=request,
                        base_completed=completed_bytes,
                        total_bytes=plan.total_bytes,
                        item_index=index,
                        item_total=len(plan.assets),
                    )
                    completed_bytes += written

                manifest = {
                    "format": SOLIN_PLAYLIST_FORMAT,
                    "schema_version": SOLIN_PLAYLIST_SCHEMA_VERSION,
                    "generator": {
                        "name": "Solin",
                        "version": request.generator_version,
                    },
                    "created_at": datetime.now(UTC).isoformat(),
                    "playlist": plan.playlist,
                    "sources": plan.sources,
                    "assets": [asset.descriptor() for asset in plan.assets],
                }
                validate_native_manifest(manifest)
                manifest_bytes = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(manifest_bytes) > SOLIN_PLAYLIST_MANIFEST_MAX_BYTES:
                    raise NativePlaylistValidationError(
                        "Solin playlist manifest exceeds the 16 MiB limit"
                    )
                archive.writestr(
                    _zip_info(_MANIFEST_ENTRY),
                    manifest_bytes,
                    compress_type=zipfile.ZIP_STORED,
                )
            raw.flush()
            os.fsync(raw.fileno())

        _check_cancelled(request.cancellation)
        _emit(
            request,
            "Finalizing export",
            completed=plan.total_bytes,
            total=plan.total_bytes,
            items_completed=len(plan.assets),
            items_total=len(plan.assets),
            bytes_completed=plan.total_bytes,
            bytes_total=plan.total_bytes,
            can_cancel=False,
        )
        os.replace(temp_path, output_path)
        _sync_parent_directory(output_path.parent)
    except Exception:  # noqa: BLE001 - atomic export cleanup boundary
        _unlink_quietly(temp_path)
        raise

    return NativePlaylistExportResult(
        output_path=output_path,
        assets_written=len(plan.assets),
        embedded_media=plan.embedded_media,
        thumbnails=plan.thumbnails,
        bytes_written=completed_bytes,
    )


def import_native_playlist(
    request: NativePlaylistImportRequest,
) -> NativePlaylistImportResult:
    """Validate, stream, and commit one native playlist into profile-owned storage."""
    input_path = Path(request.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    _check_cancelled(request.cancellation)
    _emit(request, "Validating playlist", detail=input_path.name)

    embedded_dir = Path(request.embedded_media_dir)
    thumbnail_dir = Path(request.thumbnail_cache_dir)
    embedded_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    if request.staging_dir is not None:
        common_stage = Path(request.staging_dir) / session_id
        media_stage = common_stage / "media"
        thumbnail_stage = common_stage / "thumbnails"
    else:
        media_stage = embedded_dir / _STAGING_DIR_NAME / session_id
        thumbnail_stage = thumbnail_dir / _STAGING_DIR_NAME / session_id
    stage_dirs = {media_stage, thumbnail_stage}
    created_files: list[Path] = []

    try:
        with zipfile.ZipFile(input_path, "r", allowZip64=True) as archive:
            infos = _validate_archive_index(archive)
            manifest = _read_and_validate_manifest(archive, infos)
            normalized_name = (
                request.validate_playlist_name(manifest["playlist"].get("name"))
                if request.validate_playlist_name is not None
                else None
            )
            assets = _validate_declared_assets(manifest, infos)
            _ensure_free_space(assets, embedded_dir, thumbnail_dir)
            playlist, item_id_map = regenerate_playlist_ids(manifest["playlist"])
            if normalized_name is not None:
                playlist["name"] = normalized_name
            resolved_sources = _resolve_import_sources(manifest["sources"], request)
            _assign_import_destinations(
                assets,
                item_id_map=item_id_map,
                embedded_dir=embedded_dir,
                thumbnail_dir=thumbnail_dir,
                media_stage=media_stage,
                thumbnail_stage=thumbnail_stage,
            )

            total_bytes = sum(asset.descriptor["size"] for asset in assets)
            completed_bytes = 0
            for index, asset in enumerate(assets):
                _check_cancelled(request.cancellation)
                asset.stage_path.parent.mkdir(parents=True, exist_ok=True)
                _emit(
                    request,
                    "Importing media",
                    detail=asset.descriptor["original_filename"],
                    completed=completed_bytes,
                    total=total_bytes,
                    items_completed=index,
                    items_total=len(assets),
                    bytes_completed=completed_bytes,
                    bytes_total=total_bytes,
                )
                written = _extract_asset_streaming(
                    archive,
                    asset,
                    request=request,
                    base_completed=completed_bytes,
                    total_bytes=total_bytes,
                    item_index=index,
                    item_total=len(assets),
                )
                completed_bytes += written

            _apply_sources_to_playlist(
                playlist,
                old_to_new_item_ids=item_id_map,
                sources=resolved_sources,
                assets=assets,
            )
            _check_cancelled(request.cancellation)
            _emit(
                request,
                "Finalizing import",
                completed=total_bytes,
                total=total_bytes,
                items_completed=len(assets),
                items_total=len(assets),
                bytes_completed=total_bytes,
                bytes_total=total_bytes,
                can_cancel=False,
            )
            for asset in assets:
                destination = asset.destination
                if destination is None:
                    raise NativePlaylistValidationError("Import destination was not assigned")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"Import destination already exists: {destination}")
                os.replace(asset.stage_path, destination)
                created_files.append(destination)

        return NativePlaylistImportResult(
            playlist=playlist,
            created_files=tuple(created_files),
            source_path=input_path,
            assets_imported=len(assets),
            bytes_imported=sum(asset.descriptor["size"] for asset in assets),
        )
    except (
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        NotImplementedError,
        RuntimeError,
    ):
        for created in reversed(created_files):
            _unlink_quietly(created)
        raise
    finally:
        for directory in stage_dirs:
            _remove_stage_session(directory)


def rollback_native_playlist_import(result: NativePlaylistImportResult) -> None:
    """Remove only files created by a completed import whose persistence failed."""
    failures: list[Path] = []
    for path in reversed(result.created_files):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failures.append(path)
    if failures:
        paths = ", ".join(os.fspath(path) for path in failures)
        raise OSError(f"Could not roll back imported playlist files: {paths}")


def cleanup_stale_native_playlist_imports(
    embedded_media_dir: str | Path,
    thumbnail_cache_dir: str | Path,
    *,
    older_than_seconds: float = 24 * 60 * 60,
) -> int:
    """Remove abandoned import sessions from the two profile-owned staging roots."""
    cutoff = time.time() - max(0.0, older_than_seconds)
    removed = 0
    roots = {
        Path(embedded_media_dir) / _STAGING_DIR_NAME,
        Path(thumbnail_cache_dir) / _STAGING_DIR_NAME,
    }
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime <= cutoff:
                    shutil.rmtree(child)
                    removed += 1
            except OSError:
                continue
        _remove_empty_directory(root)
    return removed


def _build_export_plan(request: NativePlaylistExportRequest) -> _ExportPlan:
    playlist = portable_playlist_snapshot(request.playlist)
    original_items = request.playlist.get("items")
    if not isinstance(original_items, list):
        raise NativePlaylistValidationError("Playlist items must be an array")

    sources: list[dict[str, Any]] = []
    assets: list[_AssetPlan] = []
    assets_by_file: dict[str, _AssetPlan] = {}
    missing_paths: list[str] = []
    embedded_media = 0

    for item in original_items:
        if not isinstance(item, dict):
            raise NativePlaylistValidationError("Playlist items must be objects")
        item_id = str(item["id"])
        raw_url = item.get("url")
        if not isinstance(raw_url, str):
            raise NativePlaylistValidationError("Playlist item url must be a string")
        url = raw_url.strip()
        remote = _is_remote_url(url)
        local_path: Path | None = None
        origin_url: str | None = None
        if remote:
            cached = completed_cached_path(url, request.media_cache_dir)
            if cached is not None:
                local_path = Path(cached)
                candidate_origin = item.get("source_url")
                origin_url = (
                    candidate_origin
                    if isinstance(candidate_origin, str) and _is_remote_url(candidate_origin)
                    else url
                )
        elif url:
            local_path = _local_path_from_location(url)
            candidate_origin = item.get("source_url")
            if isinstance(candidate_origin, str) and _is_remote_url(candidate_origin):
                origin_url = candidate_origin

        if local_path is not None:
            try:
                asset = _media_asset_plan(
                    local_path,
                    item_id,
                    media_type=str(item.get("type") or ""),
                )
            except OSError:
                missing_paths.append(os.fspath(local_path))
                continue
            file_key = os.path.normcase(os.fspath(local_path.resolve()))
            existing = assets_by_file.get(file_key)
            if existing is None:
                assets_by_file[file_key] = asset
                assets.append(asset)
                selected = asset
                embedded_media += 1
            else:
                if item_id not in existing.item_ids:
                    existing.item_ids.append(item_id)
                selected = existing
            source: dict[str, Any] = {
                "item_id": item_id,
                "kind": "embedded",
                "asset_id": selected.identifier,
            }
            if origin_url:
                source["origin_url"] = origin_url
            sources.append(source)
            continue

        if not remote:
            missing_paths.append(url or f"{item.get('title') or item_id} (empty location)")
            continue

        reference = _jw_reference(item, url)
        if reference is not None:
            sources.append(
                {
                    "item_id": item_id,
                    "kind": "jw",
                    "reference": reference,
                    "fallback_url": url,
                }
            )
        else:
            sources.append({"item_id": item_id, "kind": "direct", "url": url})

    if missing_paths:
        raise NativePlaylistMissingMediaError(list(dict.fromkeys(missing_paths)))

    thumbnail_count = 0
    thumbnail_root = Path(request.thumbnail_cache_dir)
    for item in original_items:
        item_id = str(item["id"])
        path = thumbnail_root / f"{item_id}.jpg"
        try:
            if not path.is_file():
                continue
            asset = _asset_plan(path, "thumbnail", [item_id], mime_type="image/jpeg")
        except OSError:
            continue
        assets.append(asset)
        thumbnail_count += 1

    return _ExportPlan(
        playlist=playlist,
        sources=sources,
        assets=assets,
        embedded_media=embedded_media,
        thumbnails=thumbnail_count,
        total_bytes=sum(asset.size for asset in assets),
    )


def _media_asset_plan(path: Path, item_id: str, *, media_type: str) -> _AssetPlan:
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    if not mime_type.startswith(("audio/", "image/", "video/")):
        mime_type = {
            "audio": "audio/mpeg",
            "image": "image/jpeg",
            "video": "video/mp4",
        }.get(media_type, "")
    if not mime_type:
        raise NativePlaylistValidationError(f"Unsupported playlist media type: {path}")
    return _asset_plan(path, "media", [item_id], mime_type=mime_type)


def _asset_plan(
    path: Path,
    role: str,
    item_ids: list[str],
    *,
    mime_type: str,
) -> _AssetPlan:
    with path.open("rb") as handle:
        file_stat = os.fstat(handle.fileno())
    if not stat.S_ISREG(file_stat.st_mode):
        raise NativePlaylistValidationError(f"Asset is not a regular file: {path}")
    identifier = str(uuid.uuid4())
    suffix = _safe_suffix(path.suffix, mime_type)
    return _AssetPlan(
        identifier=identifier,
        archive_path=f"assets/{identifier}{suffix}",
        role=role,
        item_ids=list(item_ids),
        source_path=path,
        original_filename=path.name[:255],
        mime_type=mime_type,
        size=file_stat.st_size,
        signature=_file_signature(file_stat),
    )


def _write_asset_streaming(
    archive: zipfile.ZipFile,
    asset: _AssetPlan,
    *,
    request: NativePlaylistExportRequest,
    base_completed: int,
    total_bytes: int,
    item_index: int,
    item_total: int,
) -> int:
    digest = hashlib.sha256()
    written = 0
    info = _zip_info(asset.archive_path)
    info.file_size = asset.size
    with asset.source_path.open("rb") as source:
        if _file_signature(os.fstat(source.fileno())) != asset.signature:
            raise NativePlaylistError(f"Media changed before export: {asset.source_path}")
        with archive.open(info, "w", force_zip64=True) as destination:
            while chunk := source.read(_CHUNK_SIZE):
                _check_cancelled(request.cancellation)
                destination.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                current = base_completed + written
                _emit(
                    request,
                    "Embedding media",
                    detail=asset.original_filename,
                    completed=current,
                    total=total_bytes,
                    items_completed=item_index,
                    items_total=item_total,
                    bytes_completed=current,
                    bytes_total=total_bytes,
                )
        final_signature = _file_signature(os.fstat(source.fileno()))
    if written != asset.size or final_signature != asset.signature:
        raise NativePlaylistError(f"Media changed during export: {asset.source_path}")
    asset.sha256 = digest.hexdigest()
    return written


def _write_mimetype(archive: zipfile.ZipFile) -> None:
    archive.writestr(
        _zip_info(_MIMETYPE_ENTRY),
        SOLIN_PLAYLIST_MIME.encode("ascii"),
        compress_type=zipfile.ZIP_STORED,
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _validate_archive_index(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries:
        raise NativePlaylistValidationError("Solin playlist archive is empty")
    if len(entries) > SOLIN_PLAYLIST_MAX_ENTRIES:
        raise NativePlaylistValidationError("Solin playlist archive has too many entries")
    if entries[0].filename != _MIMETYPE_ENTRY:
        raise NativePlaylistValidationError("The mimetype entry must be first")

    by_name: dict[str, zipfile.ZipInfo] = {}
    casefold_names: set[str] = set()
    for info in entries:
        name = info.filename
        _validate_archive_name(name)
        folded = name.casefold()
        if name in by_name or folded in casefold_names:
            raise NativePlaylistValidationError(f"Duplicate archive entry: {name}")
        if info.is_dir():
            raise NativePlaylistValidationError("Directory entries are not allowed")
        if info.flag_bits & 0x1:
            raise NativePlaylistValidationError("Encrypted archive entries are not allowed")
        mode = info.external_attr >> 16
        if mode:
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise NativePlaylistValidationError("Symbolic links are not allowed")
            if file_type not in {0, stat.S_IFREG}:
                raise NativePlaylistValidationError("Non-regular archive entries are not allowed")
        by_name[name] = info
        casefold_names.add(folded)

    mimetype_info = by_name.get(_MIMETYPE_ENTRY)
    if mimetype_info is None or mimetype_info.compress_type != zipfile.ZIP_STORED:
        raise NativePlaylistValidationError("The mimetype entry must be stored")
    mimetype = _read_limited(archive, mimetype_info, len(SOLIN_PLAYLIST_MIME) + 1)
    if mimetype != SOLIN_PLAYLIST_MIME.encode("ascii"):
        raise NativePlaylistValidationError("Invalid Solin playlist MIME marker")
    if _MANIFEST_ENTRY not in by_name:
        raise NativePlaylistValidationError("Solin playlist manifest is missing")
    return by_name


def _read_and_validate_manifest(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
) -> dict[str, Any]:
    info = infos[_MANIFEST_ENTRY]
    if info.file_size > SOLIN_PLAYLIST_MANIFEST_MAX_BYTES:
        raise NativePlaylistValidationError("Solin playlist manifest exceeds 16 MiB")
    payload = _read_limited(archive, info, SOLIN_PLAYLIST_MANIFEST_MAX_BYTES)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda constant: (_raise_invalid_json_constant(constant)),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativePlaylistValidationError("Solin playlist manifest is invalid JSON") from exc
    return validate_native_manifest(value)


def _validate_declared_assets(
    manifest: dict[str, Any],
    infos: dict[str, zipfile.ZipInfo],
) -> list[_ImportAsset]:
    declared_names = {_MIMETYPE_ENTRY, _MANIFEST_ENTRY}
    assets: list[_ImportAsset] = []
    for descriptor in manifest["assets"]:
        path = descriptor["path"]
        declared_names.add(path)
        info = infos.get(path)
        if info is None:
            raise NativePlaylistValidationError(f"Declared asset is missing: {path}")
        if info.compress_type != zipfile.ZIP_STORED:
            raise NativePlaylistValidationError(f"Asset must be stored, not compressed: {path}")
        if info.file_size != descriptor["size"]:
            raise NativePlaylistValidationError(f"Asset size differs from manifest: {path}")
        assets.append(_ImportAsset(descriptor=descriptor, info=info, stage_path=Path()))
    extras = set(infos) - declared_names
    if extras:
        raise NativePlaylistValidationError(
            f"Archive contains undeclared entries: {', '.join(sorted(extras))}"
        )
    return assets


def _assign_import_destinations(
    assets: list[_ImportAsset],
    *,
    item_id_map: dict[str, str],
    embedded_dir: Path,
    thumbnail_dir: Path,
    media_stage: Path,
    thumbnail_stage: Path,
) -> None:
    for asset in assets:
        descriptor = asset.descriptor
        if descriptor["role"] == "thumbnail":
            old_item_id = descriptor["item_ids"][0]
            filename = f"{item_id_map[old_item_id]}.jpg"
            asset.stage_path = thumbnail_stage / filename
            asset.destination = thumbnail_dir / filename
        else:
            suffix = _safe_suffix(
                Path(descriptor["original_filename"]).suffix,
                descriptor["mime_type"],
            )
            filename = f"{uuid.uuid4().hex}{suffix}"
            asset.stage_path = media_stage / filename
            asset.destination = embedded_dir / filename


def _extract_asset_streaming(
    archive: zipfile.ZipFile,
    asset: _ImportAsset,
    *,
    request: NativePlaylistImportRequest,
    base_completed: int,
    total_bytes: int,
    item_index: int,
    item_total: int,
) -> int:
    digest = hashlib.sha256()
    written = 0
    try:
        with archive.open(asset.info, "r") as source, asset.stage_path.open("xb") as destination:
            while chunk := source.read(_CHUNK_SIZE):
                _check_cancelled(request.cancellation)
                destination.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if written > asset.descriptor["size"]:
                    raise NativePlaylistValidationError("Asset exceeds its declared size")
                current = base_completed + written
                _emit(
                    request,
                    "Importing media",
                    detail=asset.descriptor["original_filename"],
                    completed=current,
                    total=total_bytes,
                    items_completed=item_index,
                    items_total=item_total,
                    bytes_completed=current,
                    bytes_total=total_bytes,
                )
            destination.flush()
            os.fsync(destination.fileno())
    except zipfile.BadZipFile as exc:
        raise NativePlaylistValidationError("Asset CRC validation failed") from exc
    if written != asset.descriptor["size"]:
        raise NativePlaylistValidationError("Asset size differs from manifest")
    if digest.hexdigest() != asset.descriptor["sha256"]:
        raise NativePlaylistValidationError("Asset SHA-256 differs from manifest")
    return written


def _resolve_import_sources(
    sources: list[dict[str, Any]],
    request: NativePlaylistImportRequest,
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    jw_sources = [source for source in sources if source["kind"] == "jw"]
    jw_index = 0
    for source in sources:
        _check_cancelled(request.cancellation)
        copied = dict(source)
        if copied["kind"] == "jw":
            fallback = copied.get("fallback_url")
            url: str | None = None
            if request.resolve_jw_source is not None:
                _emit(
                    request,
                    "Resolving JW media",
                    detail=str(copied["item_id"]),
                    completed=jw_index,
                    total=len(jw_sources),
                    items_completed=jw_index,
                    items_total=len(jw_sources),
                )
                try:
                    url = request.resolve_jw_source(copied)
                except Exception as exc:  # noqa: BLE001 - injected network adapter boundary
                    if not fallback:
                        raise NativePlaylistError("Could not resolve JW media") from exc
            url = url or fallback
            if not isinstance(url, str) or not _is_remote_url(url):
                raise NativePlaylistError("Could not resolve JW media")
            copied["resolved_url"] = url
            jw_index += 1
        resolved[copied["item_id"]] = copied
    return resolved


def _apply_sources_to_playlist(
    playlist: dict[str, Any],
    *,
    old_to_new_item_ids: dict[str, str],
    sources: dict[str, dict[str, Any]],
    assets: list[_ImportAsset],
) -> None:
    asset_paths = {
        asset.descriptor["id"]: asset.destination
        for asset in assets
        if asset.descriptor["role"] == "media"
    }
    items_by_new_id = {item["id"]: item for item in playlist["items"]}
    for old_item_id, new_item_id in old_to_new_item_ids.items():
        item = items_by_new_id[new_item_id]
        source = sources[old_item_id]
        kind = source["kind"]
        if kind == "embedded":
            destination = asset_paths[source["asset_id"]]
            if destination is None:
                raise NativePlaylistValidationError("Embedded media destination is missing")
            item["url"] = os.fspath(destination)
            origin_url = source.get("origin_url")
            if origin_url:
                item["source_url"] = origin_url
            else:
                item.pop("source_url", None)
        elif kind == "direct":
            item["url"] = source["url"]
            item.pop("source_url", None)
        else:
            item["url"] = source["resolved_url"]
            item.pop("source_url", None)


def _ensure_free_space(
    assets: list[_ImportAsset],
    embedded_dir: Path,
    thumbnail_dir: Path,
) -> None:
    required_by_volume: dict[str, int] = {}
    roots_by_volume: dict[str, Path] = {}
    for role, root in (("media", embedded_dir), ("thumbnail", thumbnail_dir)):
        volume = _volume_key(root)
        roots_by_volume.setdefault(volume, root)
        required_by_volume[volume] = required_by_volume.get(volume, 0) + sum(
            asset.descriptor["size"]
            for asset in assets
            if asset.descriptor["role"] == role
        )
    for volume, required in required_by_volume.items():
        if required <= 0:
            continue
        margin = max(_MIN_FREE_BYTES, (required + 19) // 20)
        free = shutil.disk_usage(roots_by_volume[volume]).free
        if free < required + margin:
            raise OSError(
                f"Not enough free space to import playlist: need {required + margin} bytes"
            )


def _volume_key(path: Path) -> str:
    try:
        return f"device:{path.stat().st_dev}"
    except OSError:
        return f"anchor:{path.resolve().anchor.casefold()}"


def _read_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    data = bytearray()
    with archive.open(info, "r") as source:
        while chunk := source.read(min(_CHUNK_SIZE, limit + 1 - len(data))):
            data.extend(chunk)
            if len(data) > limit:
                raise NativePlaylistValidationError("Archive entry exceeds its size limit")
    return bytes(data)


def _validate_archive_name(name: str) -> None:
    if not name or "\x00" in name or "\\" in name or "//" in name:
        raise NativePlaylistValidationError(f"Unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise NativePlaylistValidationError(f"Unsafe archive path: {name!r}")
    if ":" in path.parts[0]:
        raise NativePlaylistValidationError(f"Unsafe archive path: {name!r}")


def _jw_reference(item: dict[str, Any], url: str) -> dict[str, Any] | None:
    parsed = parse_jw_media_reference(
        url,
        original_filename=str(item.get("original_filename") or ""),
    ) or {}
    reference: dict[str, Any] = {}
    for key in ("key_symbol", "track", "issue_tag", "doc_id", "meps_language"):
        value = item.get(key)
        if value is None:
            value = parsed.get(key)
        if value is not None:
            reference[key] = value
    has_identity = bool(reference.get("key_symbol") or reference.get("doc_id"))
    if not has_identity:
        return None
    if not is_jw_url(url) and not parsed and not item.get("jw_media_id"):
        return None
    return reference


def _safe_suffix(suffix: str, mime_type: str) -> str:
    normalized = suffix.lower()
    if _SAFE_SUFFIX_RE.fullmatch(normalized) and normalized in MEDIA_EXTS:
        return normalized
    fallback = mime_to_ext(mime_type)
    return fallback if _SAFE_SUFFIX_RE.fullmatch(fallback) else ""


def _local_path_from_location(location: str) -> Path:
    parsed = urlsplit(location)
    if parsed.scheme.lower() != "file":
        return Path(location)
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return Path(path)


def _is_remote_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _file_signature(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_size, value.st_mtime_ns, value.st_ino)


def _emit(
    request: NativePlaylistExportRequest | NativePlaylistImportRequest,
    stage: str,
    *,
    detail: str = "",
    completed: int = 0,
    total: int = 0,
    can_cancel: bool = True,
    items_completed: int = 0,
    items_total: int = 0,
    bytes_completed: int = 0,
    bytes_total: int = 0,
) -> None:
    if request.progress is None:
        return
    request.progress(
        NativePlaylistProgress(
            stage=stage,
            detail=detail,
            completed=completed,
            total=total,
            can_cancel=can_cancel,
            items_completed=items_completed,
            items_total=items_total,
            bytes_completed=bytes_completed,
            bytes_total=bytes_total,
        )
    )


def _check_cancelled(cancellation: CancellationProbe | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise NativePlaylistTransferCancelled("Playlist transfer cancelled")


def _raise_invalid_json_constant(constant: str) -> None:
    raise NativePlaylistValidationError(f"Invalid JSON constant: {constant}")


def _remove_stage_session(directory: Path) -> None:
    try:
        shutil.rmtree(directory, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        return
    _remove_empty_directory(directory.parent)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _sync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "cleanup_stale_native_playlist_imports",
    "export_native_playlist",
    "import_native_playlist",
    "rollback_native_playlist_import",
]
