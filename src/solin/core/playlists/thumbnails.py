from __future__ import annotations

from pathlib import Path


def playlist_thumb_path(
    item_id: str,
    *,
    thumb_cache_dir: str | Path,
) -> Path:
    return Path(thumb_cache_dir) / f"{item_id}.jpg"
