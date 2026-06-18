"""Application service for exporting JW Library playlist documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.playlists.jwl_files import write_jwlplaylist_document


@dataclass(frozen=True, slots=True)
class JwlPlaylistExportRequest:
    name: str
    items: list[dict[str, Any]]
    output_path: str | Path
    media_cache_dir: str | Path
    fallback_lang_code: str = "E"


def export_jwlplaylist_document(request: JwlPlaylistExportRequest) -> None:
    """Write an exported playlist document from an explicit request object."""
    write_jwlplaylist_document(
        request.name,
        list(request.items),
        request.output_path,
        request.media_cache_dir,
        fallback_lang_code=request.fallback_lang_code,
    )
