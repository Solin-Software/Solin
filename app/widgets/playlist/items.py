from __future__ import annotations

import re
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap

from ...core.foundation.constants import AUDIO_EXTS as _AUDIO_EXTS
from ...core.foundation.constants import IMAGE_EXTS as _IMAGE_EXTS
from ...core.playlists.writer import parse_jworg_url
from ...core.jw.metadata import MEPS_FROM_LANG as _MEPS
from ..media_info_extractor import is_filename_title
from .thumbnails import _load_thumb_from_disk, _thumb_to_bytes


def media_type_from_url(url: str) -> str:
    if not url:
        return "video"
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "video"


def _enrich_items_for_export(
    items: list[dict],
    thumb_cache: "dict[str, QPixmap]",
) -> list[dict]:
    result = []
    for item in items:
        enriched = dict(item)
        item_id = item.get("id", "")

        if not enriched.get("thumbnail_data"):
            pixmap = thumb_cache.get(item_id)
            if pixmap is None or pixmap.isNull():
                pixmap = _load_thumb_from_disk(item_id)
            if pixmap is not None and not pixmap.isNull():
                enriched["thumbnail_data"] = _thumb_to_bytes(pixmap)

        result.append(enriched)
    return result


def new_playlist_item(title: str, url: str, **kwargs) -> dict:
    media_type = kwargs.pop("type", media_type_from_url(url))
    url_stem = Path(url.split("?")[0]).stem if url else ""
    is_auto = is_filename_title(title) or (bool(url_stem) and title == url_stem)
    item = {
        "id": str(uuid.uuid4()),
        "title": title,
        "url": url,
        "type": media_type,
        "auto_title": is_auto,
        "key_symbol": None,
        "track": None,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 0,
    }
    for key, value in kwargs.items():
        item[key] = value

    if not item.get("key_symbol") and url:
        parsed = parse_jworg_url(url)
        if not parsed and not url.startswith(("http://", "https://")):
            stem = Path(url.split("?")[0]).stem
            match = re.match(
                r"^([A-Za-z][A-Za-z0-9]{1,11})_([A-Z]{1,4})_(\d{1,4})(?:_.*)?$",
                stem,
                re.IGNORECASE,
            )
            if match:
                parsed = {
                    "key_symbol": match.group(1).lower(),
                    "track": int(match.group(3)),
                    "issue_tag": None,
                    "doc_id": None,
                    "meps_language": _MEPS.get(match.group(2).upper(), 0),
                }
            else:
                match_doc = re.match(
                    r"^(\d{5,12})_([A-Z]{1,4})_[A-Za-z]+_(\d+)",
                    stem,
                    re.IGNORECASE,
                )
                if match_doc:
                    parsed = {
                        "key_symbol": None,
                        "track": int(match_doc.group(3)),
                        "issue_tag": None,
                        "doc_id": int(match_doc.group(1)),
                        "meps_language": _MEPS.get(match_doc.group(2).upper(), 0),
                    }
        if parsed:
            item.update(parsed)
            if parsed.get("key_symbol") or parsed.get("doc_id"):
                item["auto_title"] = True
    return item


def _rounded_pixmap(pixmap: QPixmap, r: int = 5) -> QPixmap:
    if pixmap.isNull():
        return pixmap
    out = QPixmap(pixmap.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pixmap.width(), pixmap.height(), r, r)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return out
