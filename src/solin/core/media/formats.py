"""Canonical media formats and framework-independent classification."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


VIDEO_EXTS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",
        ".wmv",
        ".flv",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".ts",
        ".mts",
        ".m2ts",
        ".3gp",
        ".ogv",
    }
)
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".aac",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
        ".wma",
        ".opus",
    }
)
IMAGE_EXTS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".tif",
        ".ico",
        ".svg",
    }
)
MEDIA_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "video/mpeg": ".mpeg",
    "video/ogg": ".ogv",
    "video/3gpp": ".3gp",
    "video/mp2t": ".ts",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/aac": ".aac",
    "audio/opus": ".opus",
    "audio/x-ms-wma": ".wma",
}


def media_kind_from_extension(extension: str) -> MediaKind:
    normalized = extension.lower().strip()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized in VIDEO_EXTS:
        return MediaKind.VIDEO
    if normalized in AUDIO_EXTS:
        return MediaKind.AUDIO
    if normalized in IMAGE_EXTS:
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def media_kind_from_path(path: str | Path) -> MediaKind:
    raw = str(path)
    parsed_path = urlsplit(raw).path if "://" in raw or "?" in raw else raw
    return media_kind_from_extension(Path(parsed_path).suffix)


def media_kind_from_mime(mime_type: str | None) -> MediaKind:
    normalized = (mime_type or "").partition(";")[0].lower().strip()
    if normalized.startswith("video/"):
        return MediaKind.VIDEO
    if normalized.startswith("audio/"):
        return MediaKind.AUDIO
    if normalized.startswith("image/"):
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def media_type_from_path(path: str | Path, *, default: str = "unknown") -> str:
    kind = media_kind_from_path(path)
    return default if kind is MediaKind.UNKNOWN else kind.value


def mime_to_ext(mime_type: str | None) -> str:
    """Return a safe extension for image, video, and audio MIME types."""
    normalized = (mime_type or "").partition(";")[0].lower().strip()
    if normalized in _MIME_TO_EXT:
        return _MIME_TO_EXT[normalized]
    kind = media_kind_from_mime(normalized)
    if kind is MediaKind.AUDIO:
        return ".mp3"
    if kind is MediaKind.VIDEO:
        return ".mp4"
    return ".jpg"
