"""Qt-only rendering and thumbnail enrichment for playlist items."""

from __future__ import annotations

from PySide6.QtGui import QPixmap

from ...core.media.thumbnail_store import ThumbnailStore
from ...ui.thumbnail_images import load_thumbnail, pixmap_to_jpeg_bytes


def enrich_items_for_export(
    items: list[dict],
    thumb_cache: dict[str, QPixmap],
    thumbnail_store: ThumbnailStore,
) -> list[dict]:
    result = []
    for item in items:
        enriched = dict(item)
        item_id = item.get("id", "")

        if not enriched.get("thumbnail_data"):
            pixmap = thumb_cache.get(item_id)
            if pixmap is None or pixmap.isNull():
                pixmap = load_thumbnail(thumbnail_store, item_id)
            if pixmap is not None and not pixmap.isNull():
                enriched["thumbnail_data"] = pixmap_to_jpeg_bytes(pixmap)

        result.append(enriched)
    return result
