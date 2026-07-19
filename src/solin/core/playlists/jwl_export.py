"""Application service for exporting JW Library playlist documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.playlists.jwl_files import (
    CancelCallback,
    ProgressCallback,
    write_jwlplaylist_document,
)


@dataclass(frozen=True, slots=True)
class JwlPlaylistExportRequest:
    name: str
    items: list[dict[str, Any]]
    output_path: str | Path
    media_cache_dir: str | Path
    fallback_lang_code: str = "E"
    progress_callback: ProgressCallback | None = None
    should_cancel: CancelCallback | None = None


def export_jwlplaylist_document(request: JwlPlaylistExportRequest) -> None:
    """Write an exported playlist document from an explicit request object."""
    options: dict[str, Any] = {"fallback_lang_code": request.fallback_lang_code}
    if request.progress_callback is not None:
        options["progress_callback"] = request.progress_callback
    if request.should_cancel is not None:
        options["should_cancel"] = request.should_cancel
    write_jwlplaylist_document(
        request.name,
        list(request.items),
        request.output_path,
        request.media_cache_dir,
        **options,
    )
