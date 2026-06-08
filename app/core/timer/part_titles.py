"""Canonical timer part title sources.

This module is intentionally Qt-free so schedule construction/normalization
stays pure. Translation markers and display formatting live in
``app.core.i18n.timer_part_titles``.
"""

from __future__ import annotations

TREASURES_TALK_TITLE = "Treasures Talk"
SPIRITUAL_GEMS_TITLE = "Spiritual Gems"
BIBLE_READING_TITLE = "Bible Reading"
PUBLIC_TALK_TITLE = "Public Talk"
WATCHTOWER_STUDY_TITLE = "Watchtower Study"
CBS_TITLE = "Congregation Bible Study"
OPENING_COMMENTS_TITLE = "Opening Comments"
CONCLUDING_COMMENTS_TITLE = "Concluding Comments"

# Stored as canonical source titles for indexed default timer parts. The number
# is inserted only when rendering UI/PDF text.
GENERIC_INDEXED_PART_TITLE = "Part {number}"
TREASURES_INDEXED_PART_TITLE = "Treasures Part {number}"
PUBLIC_TALK_INDEXED_PART_TITLE = "Public Talk {number}"
WATCHTOWER_STUDY_INDEXED_PART_TITLE = "Watchtower Study {number}"

STATIC_PART_TITLE_SOURCES = frozenset({
    TREASURES_TALK_TITLE,
    SPIRITUAL_GEMS_TITLE,
    BIBLE_READING_TITLE,
    PUBLIC_TALK_TITLE,
    WATCHTOWER_STUDY_TITLE,
    CBS_TITLE,
    OPENING_COMMENTS_TITLE,
    CONCLUDING_COMMENTS_TITLE,
})

INDEXED_PART_TITLE_SOURCES = frozenset({
    GENERIC_INDEXED_PART_TITLE,
    TREASURES_INDEXED_PART_TITLE,
    PUBLIC_TALK_INDEXED_PART_TITLE,
    WATCHTOWER_STUDY_INDEXED_PART_TITLE,
})

PART_TITLE_SOURCES = (
    TREASURES_TALK_TITLE,
    SPIRITUAL_GEMS_TITLE,
    BIBLE_READING_TITLE,
    PUBLIC_TALK_TITLE,
    WATCHTOWER_STUDY_TITLE,
    CBS_TITLE,
    OPENING_COMMENTS_TITLE,
    CONCLUDING_COMMENTS_TITLE,
    GENERIC_INDEXED_PART_TITLE,
    TREASURES_INDEXED_PART_TITLE,
    PUBLIC_TALK_INDEXED_PART_TITLE,
    WATCHTOWER_STUDY_INDEXED_PART_TITLE,
)


def is_indexed_part_title_source(title: str) -> bool:
    return title in INDEXED_PART_TITLE_SOURCES


def is_static_part_title_source(title: str) -> bool:
    return title in STATIC_PART_TITLE_SOURCES
