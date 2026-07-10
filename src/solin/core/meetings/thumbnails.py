from __future__ import annotations

import hashlib
from pathlib import Path


def meeting_thumb_cache_key(item_id: str) -> str:
    return f"{item_id}.jpg"


def meeting_thumb_storage_id(tree_key: str, item_id: str) -> str:
    """Return a cache-safe thumbnail identity scoped to one meeting tree."""

    if not tree_key or not item_id:
        return item_id
    digest = hashlib.sha256(
        f"{tree_key}\x00{item_id}".encode("utf-8")
    ).hexdigest()
    return f"meeting-{digest}"


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
