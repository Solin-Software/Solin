"""Qt-only rendering and thumbnail enrichment for playlist items."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QPixmap

from .thumbnails import load_thumb_from_disk, thumb_to_bytes


def enrich_items_for_export(
    items: list[dict],
    thumb_cache: dict[str, QPixmap],
    thumb_cache_dir: str | Path,
) -> list[dict]:
    result = []
    for item in items:
        enriched = dict(item)
        item_id = item.get("id", "")

        if not enriched.get("thumbnail_data"):
            pixmap = thumb_cache.get(item_id)
            if pixmap is None or pixmap.isNull():
                pixmap = load_thumb_from_disk(item_id, thumb_cache_dir)
            if pixmap is not None and not pixmap.isNull():
                enriched["thumbnail_data"] = thumb_to_bytes(pixmap)

        result.append(enriched)
    return result
