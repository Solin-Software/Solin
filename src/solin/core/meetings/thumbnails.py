from __future__ import annotations

from pathlib import Path


def meeting_thumb_cache_key(item_id: str) -> str:
    return f"{item_id}.jpg"


def meeting_thumb_dir(
    *,
    meeting_thumb_cache_dir: str | Path,
) -> Path:
    return Path(meeting_thumb_cache_dir)


def meeting_thumb_path(
    item_id: str,
    *,
    meeting_thumb_cache_dir: str | Path,
) -> Path:
    return meeting_thumb_dir(
        meeting_thumb_cache_dir=meeting_thumb_cache_dir,
    ) / meeting_thumb_cache_key(item_id)
