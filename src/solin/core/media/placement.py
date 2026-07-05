"""Shared placement policy for inserting media into structured lists."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from solin.core.meetings.colors import accent_from_hue

END_OF_LIST_INDEX = 2**31 - 1
PLACEMENT_PROMPT_ITEM_THRESHOLD = 13
TOP_OF_PLAYLIST_SOURCE = "Top of playlist"
END_OF_PLAYLIST_SOURCE = "End of playlist"
SECTION_SOURCE = "Section"
MEDIA_PLACEMENT_SOURCES = frozenset(
    {
        TOP_OF_PLAYLIST_SOURCE,
        END_OF_PLAYLIST_SOURCE,
        SECTION_SOURCE,
    }
)


class MediaPlacementOption(TypedDict):
    id: str
    label: str
    type: str
    color: str


def build_media_placement_options(
    structured_list: Mapping[str, Any] | None,
    *,
    translate: Callable[[str], str],
) -> list[MediaPlacementOption]:
    """Return the user-facing choices required by the shared placement policy."""

    if not structured_list:
        return []

    items = _sequence_value(structured_list.get("items"))
    sections = [
        section
        for section in _mapping_sequence(structured_list.get("sections"))
        if not section.get("parent_id")
    ]
    if len(items) <= PLACEMENT_PROMPT_ITEM_THRESHOLD and len(sections) < 2:
        return []

    options: list[MediaPlacementOption] = [
        {
            "id": "top",
            "label": translate(TOP_OF_PLAYLIST_SOURCE),
            "type": "position",
            "color": "",
        },
        {
            "id": "bottom",
            "label": translate(END_OF_PLAYLIST_SOURCE),
            "type": "position",
            "color": "",
        },
    ]
    for section in sections:
        section_id = str(section.get("id") or "")
        if not section_id:
            continue
        options.append(
            {
                "id": f"section:{section_id}",
                "label": str(section.get("name") or translate(SECTION_SOURCE)),
                "type": "section",
                "color": accent_from_hue(_int_or_default(section.get("color_hue"), 215)),
            }
        )
    return options


def resolve_media_placement(placement_id: str) -> tuple[str, int]:
    """Translate a placement choice into a list target and index."""

    if placement_id == "top":
        return "root", 0
    if placement_id.startswith("section:"):
        return placement_id, 0
    return "root", END_OF_LIST_INDEX


def _sequence_value(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return value
    return ()


def _mapping_sequence(value: object) -> list[Mapping[str, Any]]:
    return [item for item in _sequence_value(value) if isinstance(item, Mapping)]


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


__all__ = [
    "END_OF_LIST_INDEX",
    "END_OF_PLAYLIST_SOURCE",
    "MEDIA_PLACEMENT_SOURCES",
    "MediaPlacementOption",
    "SECTION_SOURCE",
    "TOP_OF_PLAYLIST_SOURCE",
    "build_media_placement_options",
    "resolve_media_placement",
]
