"""Localized display titles for semantic meeting sections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QCoreApplication

from ..meetings.section_meta import SECTION_META


def translate_meeting_section_title(source: str) -> str:
    """Translate an official meeting section source using the active Qt catalog."""

    context = "MeetingSections" if source == "PUBLIC TALK" else "_Section"
    return QCoreApplication.translate(context, source)


def display_meeting_section_title(node: Mapping[str, Any]) -> str:
    """Return the current-locale title without overriding user-authored titles."""

    title = str(node.get("title") or "")
    if (
        node.get("type") not in ("section", "subsection")
        or not node.get("meeting_generated")
        or node.get("user_title_override")
    ):
        return title

    source_key = str(node.get("meeting_source_key") or "")
    section_code = str(node.get("section_code") or source_key.rsplit(":", 1)[-1])
    source = SECTION_META.get(section_code, ("", 0))[0]
    return translate_meeting_section_title(source) if source else title
