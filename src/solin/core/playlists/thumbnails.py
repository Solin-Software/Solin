from __future__ import annotations

from pathlib import Path

from solin.core.foundation.runtime_paths import RuntimePaths


def playlist_thumb_path(
    item_id: str,
    *,
    thumb_cache_dir: str | Path | None = None,
) -> Path:
    root = (
        Path(thumb_cache_dir)
        if thumb_cache_dir is not None
        else RuntimePaths.from_legacy_globals().thumb_cache_dir
    )
    return root / f"{item_id}.jpg"
