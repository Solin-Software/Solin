"""Canonical translations for shared media-placement choices."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.media.placement import MEDIA_PLACEMENT_SOURCES

MEDIA_PLACEMENT_CONTEXT = "MediaPlacement"

# Literal markers are required because lupdate cannot follow the translation
# callback passed into the Qt-free placement policy.
MEDIA_PLACEMENT_TRANSLATION_SOURCES = frozenset(
    {
        QT_TRANSLATE_NOOP("MediaPlacement", "Top of playlist"),
        QT_TRANSLATE_NOOP("MediaPlacement", "End of playlist"),
        QT_TRANSLATE_NOOP("MediaPlacement", "Section"),
    }
)


def translate_media_placement(source: str) -> str:
    if source not in MEDIA_PLACEMENT_SOURCES:
        raise ValueError(f"Unsupported media-placement translation source: {source!r}")
    return QCoreApplication.translate(MEDIA_PLACEMENT_CONTEXT, source) or source


__all__ = [
    "MEDIA_PLACEMENT_CONTEXT",
    "MEDIA_PLACEMENT_TRANSLATION_SOURCES",
    "translate_media_placement",
]
