"""Application-facing helpers for JW Library playlist files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.playlists.reader import read_jwlplaylist
from solin.core.playlists.writer import PlaylistWriteError
from solin.core.playlists.writer import write_jwlplaylist as _write_jwlplaylist

__all__ = [
    "JwlPlaylistDocument",
    "PlaylistWriteError",
    "read_jwlplaylist_document",
    "write_jwlplaylist_document",
]


@dataclass(frozen=True, slots=True)
class JwlPlaylistDocument:
    name: str
    items: list[dict[str, Any]]


def read_jwlplaylist_document(
    path: str | Path,
    *,
    fallback_lang_code: str = "E",
) -> JwlPlaylistDocument:
    data = read_jwlplaylist(path, fallback_lang_code=fallback_lang_code)
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
) -> None:
    _write_jwlplaylist(
        name,
        items,
        output_path,
        media_cache_dir,
        fallback_lang_code=fallback_lang_code,
    )
