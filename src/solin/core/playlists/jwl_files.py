"""Application-facing helpers for JW Library playlist files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.playlists.reader import read_jwlplaylist


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
