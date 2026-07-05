"""Filesystem-backed media cache listing helpers."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.media.formats import media_type_from_path


@dataclass(frozen=True, slots=True)
class CachedMediaItem:
    path: str
    filename: str
    display_title: str
    size: int
    media_type: str
    original_url: str = ""


def scan_cached_media_items(
    media_cache_dir: str | Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[CachedMediaItem]:
    cache_dir = Path(media_cache_dir)
    if not cache_dir.is_dir():
        return []

    items: list[CachedMediaItem] = []
    for path in sorted(cache_dir.iterdir(), key=lambda candidate: candidate.name):
        if is_cancelled and is_cancelled():
            break
        if path.name.endswith((".done", ".tmp")):
            continue
        if not path.is_file():
            continue
        done_path = Path(f"{path}.done")
        if not done_path.is_file():
            continue
        media_type = media_type_from_path(path, default="other")
        if media_type == "other":
            continue
        original_url = _read_original_url(done_path)
        filename = _display_filename(path, original_url)
        items.append(
            CachedMediaItem(
                path=str(path),
                filename=filename,
                display_title=_read_title(path, Path(filename).stem),
                size=_file_size(path),
                media_type=media_type,
                original_url=original_url,
            )
        )
    return items


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_title(path: Path, fallback: str) -> str:
    try:
        mutagen = importlib.import_module("mutagen")
        tags = mutagen.File(path, easy=True)
        if tags and tags.get("title"):
            value = tags["title"]
            title = value[0] if isinstance(value, list) else str(value)
            return title.strip() or fallback
    except Exception:  # noqa: BLE001 - third-party media metadata parser boundary
        log_ignored_exception(__name__, "Could not extract cached media title")
    return fallback


def _read_original_url(done_path: Path) -> str:
    try:
        content = done_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        log_ignored_exception(__name__, "Could not read cached media origin sidecar")
        return ""
    return content if content == content.strip() and content.startswith("http") else ""


def _display_filename(path: Path, original_url: str) -> str:
    if not original_url:
        return path.name
    try:
        decoded = unquote(Path(urlsplit(original_url).path).name)
        filename = decoded.replace("\\", "/").rsplit("/", 1)[-1]
        return filename if filename not in {"", ".", ".."} else path.name
    except ValueError:
        return path.name
