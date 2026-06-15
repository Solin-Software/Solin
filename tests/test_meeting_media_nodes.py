from __future__ import annotations

from solin.core.meetings.media_nodes import (
    clean_media_title,
    create_manual_media_node,
    media_ref_title,
    meeting_media_type_from_path,
    mime_for_meeting_media,
)


def test_create_manual_media_node_uses_file_stem_title_and_detects_media_type():
    node = create_manual_media_node(
        "C:/meeting/intro.mp3",
        node_id_factory=lambda: "fixed-id",
    )

    assert node["id"] == "fixed-id"
    assert node["type"] == "media"
    assert node["title"] == "intro"
    assert node["media_type"] == "audio"
    assert node["meeting_generated"] is False
    assert node["media_ref"] == {
        "multimedia_id": 0,
        "mime_type": "audio/mpeg",
        "file_path": "C:/meeting/intro.mp3",
        "label": "intro",
        "caption": "",
        "begin_ordinal": 0,
        "key_symbol": "",
        "track": 0,
        "issue_tag": 0,
        "meps_doc_id": 0,
        "section": "",
        "is_song": False,
        "cbs_article_title": "",
    }


def test_create_manual_media_node_preserves_clean_custom_title():
    node = create_manual_media_node(
        "C:/meeting/slide.unknown",
        "  Custom title  ",
        node_id_factory=lambda: "fixed-id",
    )

    assert node["title"] == "Custom title"
    assert node["media_type"] == "video"
    assert node["media_ref"]["mime_type"] == "video/*"
    assert node["media_ref"]["label"] == "Custom title"


def test_meeting_media_node_helpers_are_stable():
    assert clean_media_title("  Title  ") == "Title"
    assert meeting_media_type_from_path("photo.png") == "image"
    assert mime_for_meeting_media("photo.unknown", "image") == "image/*"
    assert media_ref_title({"caption": " Caption "}) == "Caption"
