"""Filesystem-backed media cache listing helpers."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

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
    on_batch: Callable[[list[CachedMediaItem]], None] | None = None,
    batch_size: int = 128,
) -> list[CachedMediaItem]:
    cache_dir = Path(media_cache_dir)
    if not cache_dir.is_dir():
        return []

    items: list[CachedMediaItem] = []
    pending_batch: list[CachedMediaItem] = []
    with os.scandir(cache_dir) as directory:
        entries = sorted(directory, key=lambda entry: entry.name.casefold())
    for entry in entries:
        if is_cancelled and is_cancelled():
            break
        if entry.name.endswith((".done", ".tmp")):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        done_path = Path(f"{entry.path}.done")
        try:
            done_stat = done_path.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(done_stat.st_mode):
            continue
        media_type = media_type_from_path(path, default="other")
        if media_type == "other":
            continue
        original_url = _read_original_url(done_path)
        filename = _display_filename(path, original_url)
        item = CachedMediaItem(
            path=str(path),
            filename=filename,
            display_title=Path(filename).stem,
            size=_entry_file_size(entry),
            media_type=media_type,
            original_url=original_url,
        )
        items.append(item)
        if on_batch is not None:
            pending_batch.append(item)
            if len(pending_batch) >= max(1, batch_size):
                on_batch(pending_batch)
                pending_batch = []
    if on_batch is not None and pending_batch:
        on_batch(pending_batch)
    return items


def _entry_file_size(entry: os.DirEntry[str]) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _read_original_url(done_path: Path) -> str:
    try:
        if done_path.stat(follow_symlinks=False).st_size > 16 * 1024:
            return ""
        content = done_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
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
