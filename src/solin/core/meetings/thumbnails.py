from __future__ import annotations

from pathlib import Path

from solin.core.foundation.runtime_paths import RuntimePaths


def meeting_thumb_cache_key(item_id: str) -> str:
    return f"{item_id}.jpg"


def meeting_thumb_dir(
    *,
    meeting_thumb_cache_dir: str | Path | None = None,
) -> Path:
    if meeting_thumb_cache_dir is not None:
        return Path(meeting_thumb_cache_dir)
    return RuntimePaths.from_legacy_globals().meeting_thumb_cache_dir


def meeting_thumb_path(
    item_id: str,
    *,
    meeting_thumb_cache_dir: str | Path | None = None,
) -> Path:
    return meeting_thumb_dir(
        meeting_thumb_cache_dir=meeting_thumb_cache_dir,
    ) / meeting_thumb_cache_key(item_id)
