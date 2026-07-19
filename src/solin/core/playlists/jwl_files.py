"""Application-facing helpers for JW Library playlist files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.playlists.reader import read_jwlplaylist
from solin.core.playlists.writer import PlaylistWriteError

__all__ = [
    "CancelCallback",
    "JwlPlaylistDocument",
    "PlaylistWriteError",
    "ProgressCallback",
    "read_jwlplaylist_document",
    "write_jwlplaylist_document",
]

# Framework-independent callbacks. ``completed`` and ``total`` use items or
# uncompressed bytes according to the reported phase; ``total`` may be unknown.
ProgressCallback = Callable[[str, int, int | None], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class JwlPlaylistDocument:
    name: str
    items: list[dict[str, Any]]


def read_jwlplaylist_document(
    path: str | Path,
    *,
    fallback_lang_code: str = "E",
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> JwlPlaylistDocument:
    options: dict[str, Any] = {"fallback_lang_code": fallback_lang_code}
    if progress_callback is not None:
        options["progress_callback"] = progress_callback
    if should_cancel is not None:
        options["should_cancel"] = should_cancel
    data = read_jwlplaylist(path, **options)
    return JwlPlaylistDocument(
        name=str(data.get("name") or Path(path).stem),
        items=list(data.get("items", [])),
    )


def write_jwlplaylist_document(
    name: str,
    items: list[dict[str, Any]],
    output_path: str | Path,
    media_cache_dir: str | Path,
    *,
    fallback_lang_code: str = "E",
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> None:
    from solin.core.playlists.writer import write_jwlplaylist

    options: dict[str, Any] = {"fallback_lang_code": fallback_lang_code}
    if progress_callback is not None:
        options["progress_callback"] = progress_callback
    if should_cancel is not None:
        options["should_cancel"] = should_cancel
    write_jwlplaylist(
        name,
        items,
        output_path,
        media_cache_dir,
        **options,
    )
