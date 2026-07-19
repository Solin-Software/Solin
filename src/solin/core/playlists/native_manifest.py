"""Schema-v1 manifest normalization and validation for ``.solinplaylist``."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from solin.core.playlists.native_types import (
    NativePlaylistValidationError,
    NativePlaylistVersionError,
    SOLIN_PLAYLIST_FORMAT,
    SOLIN_PLAYLIST_SCHEMA_VERSION,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_PATH_RE = re.compile(
    r"^assets/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"(\.[a-z0-9]{1,10})?$"
)
_MAX_URL_LENGTH = 16_384


def portable_playlist_snapshot(playlist: Mapping[str, Any]) -> dict[str, Any]:
    """Copy persisted JSON state while removing runtime keys and local locations."""
    normalized = _json_value(playlist, "playlist")
    if not isinstance(normalized, dict):
        raise NativePlaylistValidationError("Playlist root must be an object")
    _validate_playlist_tree(normalized)
    for item in normalized["items"]:
        item["url"] = ""
        item.pop("source_url", None)
    return normalized


def validate_native_manifest(value: object) -> dict[str, Any]:
    """Validate and return the schema-v1 manifest without coercing values."""
    if not isinstance(value, dict):
        raise NativePlaylistValidationError("Manifest root must be an object")
    if value.get("format") != SOLIN_PLAYLIST_FORMAT:
        raise NativePlaylistValidationError("Not a Solin playlist manifest")
    version = value.get("schema_version")
    if type(version) is not int:
        raise NativePlaylistValidationError("Manifest schema_version must be an integer")
    if version != SOLIN_PLAYLIST_SCHEMA_VERSION:
        raise NativePlaylistVersionError(
            f"Unsupported Solin playlist schema version: {version}"
        )

    playlist = value.get("playlist")
    if not isinstance(playlist, dict):
        raise NativePlaylistValidationError("Manifest playlist must be an object")
    _validate_json_value(playlist, "playlist")
    item_ids, _section_ids = _validate_playlist_tree(playlist)

    assets = value.get("assets")
    sources = value.get("sources")
    if not isinstance(assets, list) or not isinstance(sources, list):
        raise NativePlaylistValidationError("Manifest assets and sources must be arrays")

    assets_by_id: dict[str, dict[str, Any]] = {}
    asset_paths: set[str] = set()
    thumbnail_owners: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise NativePlaylistValidationError(f"Asset {index} must be an object")
        asset_id = _uuid_text(asset.get("id"), f"asset {index} id")
        if asset_id in assets_by_id:
            raise NativePlaylistValidationError(f"Duplicate asset id: {asset_id}")
        path = asset.get("path")
        if not isinstance(path, str) or not _ASSET_PATH_RE.fullmatch(path):
            raise NativePlaylistValidationError(f"Invalid asset path: {path!r}")
        if path.casefold() in asset_paths:
            raise NativePlaylistValidationError(f"Duplicate asset path: {path}")
        path_asset_id = _ASSET_PATH_RE.fullmatch(path).group(1)  # type: ignore[union-attr]
        if path_asset_id != asset_id:
            raise NativePlaylistValidationError("Asset id does not match its archive path")
        asset_paths.add(path.casefold())

        role = asset.get("role")
        if role not in {"media", "thumbnail"}:
            raise NativePlaylistValidationError(f"Invalid asset role: {role!r}")
        mime_type = asset.get("mime_type")
        if not isinstance(mime_type, str) or not _valid_mime_type(mime_type):
            raise NativePlaylistValidationError(f"Invalid asset MIME type: {mime_type!r}")
        if role == "thumbnail" and mime_type != "image/jpeg":
            raise NativePlaylistValidationError("Thumbnail assets must be JPEG images")
        if role == "media" and not mime_type.startswith(("audio/", "image/", "video/")):
            raise NativePlaylistValidationError("Media assets must use an audio/image/video MIME type")
        size = asset.get("size")
        if type(size) is not int or size < 0:
            raise NativePlaylistValidationError("Asset size must be a non-negative integer")
        sha256 = asset.get("sha256")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise NativePlaylistValidationError("Asset sha256 is invalid")
        original_filename = asset.get("original_filename")
        if not isinstance(original_filename, str) or not _safe_filename(original_filename):
            raise NativePlaylistValidationError("Asset original_filename is invalid")
        owners = asset.get("item_ids")
        if not isinstance(owners, list) or not owners:
            raise NativePlaylistValidationError("Asset item_ids must be a non-empty array")
        if not all(isinstance(owner, str) for owner in owners):
            raise NativePlaylistValidationError("Asset item_ids contains invalid owners")
        if len(set(owners)) != len(owners) or any(owner not in item_ids for owner in owners):
            raise NativePlaylistValidationError("Asset item_ids contains invalid owners")
        if role == "thumbnail":
            if len(owners) != 1 or owners[0] in thumbnail_owners:
                raise NativePlaylistValidationError(
                    "Each playlist item may own at most one thumbnail"
                )
            thumbnail_owners.add(owners[0])
        assets_by_id[asset_id] = asset

    sources_by_item: dict[str, dict[str, Any]] = {}
    referenced_media_assets: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise NativePlaylistValidationError(f"Source {index} must be an object")
        item_id = source.get("item_id")
        if not isinstance(item_id, str) or item_id not in item_ids:
            raise NativePlaylistValidationError("Source item_id is invalid")
        if item_id in sources_by_item:
            raise NativePlaylistValidationError(f"Duplicate source for item: {item_id}")
        kind = source.get("kind")
        if kind == "embedded":
            asset_id = source.get("asset_id")
            if not isinstance(asset_id, str):
                raise NativePlaylistValidationError("Embedded source asset is invalid")
            asset = assets_by_id.get(asset_id)
            if asset is None or asset["role"] != "media" or item_id not in asset["item_ids"]:
                raise NativePlaylistValidationError("Embedded source asset is invalid")
            referenced_media_assets.add(asset_id)
            origin_url = source.get("origin_url")
            if origin_url is not None:
                _validate_remote_url(origin_url, "Embedded source origin_url")
        elif kind == "direct":
            _validate_remote_url(source.get("url"), "Direct source url")
        elif kind == "jw":
            fallback_url = source.get("fallback_url")
            if fallback_url is not None:
                _validate_remote_url(fallback_url, "JW source fallback_url")
            reference = source.get("reference")
            if not isinstance(reference, dict) or not _valid_jw_reference(reference):
                raise NativePlaylistValidationError("JW source reference is invalid")
        else:
            raise NativePlaylistValidationError(f"Invalid source kind: {kind!r}")
        sources_by_item[item_id] = source

    if set(sources_by_item) != item_ids:
        raise NativePlaylistValidationError("Every playlist item must have exactly one source")
    declared_media_assets = {
        asset_id for asset_id, asset in assets_by_id.items() if asset["role"] == "media"
    }
    if referenced_media_assets != declared_media_assets:
        raise NativePlaylistValidationError("Manifest contains unreferenced media assets")

    _validate_json_value(value, "manifest")
    return value


def regenerate_playlist_ids(
    playlist: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Regenerate playlist/tree identifiers and remap every structural reference."""
    copied = _json_value(playlist, "playlist")
    if not isinstance(copied, dict):
        raise NativePlaylistValidationError("Playlist root must be an object")
    _validate_playlist_tree(copied)

    item_map = {item["id"]: str(uuid.uuid4()) for item in copied["items"]}
    section_map = {
        section["id"]: str(uuid.uuid4()) for section in copied.get("sections", [])
    }
    marker_map = {
        marker["id"]: str(uuid.uuid4()) for marker in copied.get("markers", [])
    }
    copied["id"] = str(uuid.uuid4())
    for item in copied["items"]:
        old_id = item["id"]
        item["id"] = item_map[old_id]
        _remap_optional(item, "section_id", section_map)
    for section in copied.get("sections", []):
        old_id = section["id"]
        section["id"] = section_map[old_id]
        _remap_optional(section, "parent_id", section_map)
    for marker in copied.get("markers", []):
        marker["id"] = marker_map[marker["id"]]
        _remap_optional(marker, "subsection_id", section_map)
    return copied, item_map


def _remap_optional(record: dict[str, Any], key: str, mapping: Mapping[str, str]) -> None:
    value = record.get(key)
    if value:
        record[key] = mapping[value]


def _validate_playlist_tree(playlist: dict[str, Any]) -> tuple[set[str], set[str]]:
    playlist_id = playlist.get("id")
    if not isinstance(playlist_id, str) or not _safe_identifier(playlist_id):
        raise NativePlaylistValidationError("Playlist id must be a non-empty string")
    name = playlist.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NativePlaylistValidationError(
            "Playlist name must be a non-empty string"
        )
    items = playlist.get("items")
    sections = playlist.get("sections", [])
    markers = playlist.get("markers", [])
    if not isinstance(items, list):
        raise NativePlaylistValidationError("Playlist items must be an array")
    if not isinstance(sections, list) or not isinstance(markers, list):
        raise NativePlaylistValidationError("Playlist items, sections, and markers must be arrays")

    item_ids = _record_ids(items, "item")
    section_ids = _record_ids(sections, "section")
    _record_ids(markers, "marker")
    for item in items:
        section_id = item.get("section_id")
        _validate_optional_reference(section_id, "Item section_id")
        if section_id and section_id not in section_ids:
            raise NativePlaylistValidationError("Item references an unknown section")
    for section in sections:
        parent_id = section.get("parent_id")
        _validate_optional_reference(parent_id, "Section parent_id")
        if parent_id and (parent_id not in section_ids or parent_id == section["id"]):
            raise NativePlaylistValidationError("Section references an invalid parent")
        if parent_id:
            parent = next(candidate for candidate in sections if candidate["id"] == parent_id)
            if parent.get("parent_id"):
                raise NativePlaylistValidationError("Playlist sections may only be nested one level")
    for marker in markers:
        subsection_id = marker.get("subsection_id")
        _validate_optional_reference(subsection_id, "Marker subsection_id")
        if subsection_id:
            if subsection_id not in section_ids:
                raise NativePlaylistValidationError("Marker references an unknown subsection")
            subsection = next(
                candidate for candidate in sections if candidate["id"] == subsection_id
            )
            if not subsection.get("parent_id"):
                raise NativePlaylistValidationError("Marker target must be a subsection")
    return item_ids, section_ids


def _record_ids(records: list[Any], label: str) -> set[str]:
    found: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise NativePlaylistValidationError(f"Playlist {label}s must be objects")
        identifier = record.get("id")
        if not isinstance(identifier, str) or not _safe_identifier(identifier):
            raise NativePlaylistValidationError(f"Playlist {label} id is invalid")
        if identifier in found:
            raise NativePlaylistValidationError(f"Duplicate playlist {label} id")
        found.add(identifier)
    return found


def _json_value(value: object, path: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise NativePlaylistValidationError(f"{path} contains a non-string key")
            if key.startswith("_"):
                continue
            result[key] = _json_value(child, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_json_value(child, f"{path}[]") for child in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise NativePlaylistValidationError(f"{path} contains a non-JSON value")


def _validate_json_value(value: object, path: str) -> None:
    _json_value(value, path)


def _uuid_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise NativePlaylistValidationError(f"{label} must be a UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise NativePlaylistValidationError(f"{label} must be a UUID string") from exc
    if value != canonical:
        raise NativePlaylistValidationError(f"{label} must use canonical UUID syntax")
    return canonical


def _safe_identifier(value: str) -> bool:
    return bool(
        value
        and len(value) <= 128
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _validate_optional_reference(value: object, label: str) -> None:
    if value is not None and value != "" and not isinstance(value, str):
        raise NativePlaylistValidationError(f"{label} must be a string or null")


def _safe_filename(value: str) -> bool:
    return bool(
        value
        and len(value) <= 255
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _valid_mime_type(value: str) -> bool:
    if len(value) > 127 or "/" not in value or any(char.isspace() for char in value):
        return False
    major, _, minor = value.partition("/")
    return bool(major and minor)


def _validate_remote_url(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH:
        raise NativePlaylistValidationError(f"{label} is invalid")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise NativePlaylistValidationError(f"{label} must be an HTTP(S) URL")


def _valid_jw_reference(reference: Mapping[str, Any]) -> bool:
    allowed = {"key_symbol", "track", "issue_tag", "doc_id", "meps_language"}
    if not set(reference).issubset(allowed):
        return False
    key_symbol = reference.get("key_symbol")
    if key_symbol is not None and (not isinstance(key_symbol, str) or not key_symbol):
        return False
    for key in ("track", "issue_tag", "doc_id", "meps_language"):
        value = reference.get(key)
        if value is not None and (type(value) is not int or value < 0):
            return False
    return bool(key_symbol or reference.get("doc_id"))


__all__ = [
    "portable_playlist_snapshot",
    "regenerate_playlist_ids",
    "validate_native_manifest",
]
