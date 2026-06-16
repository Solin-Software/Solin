from __future__ import annotations

from solin.core.meetings.media_nodes import (
    clean_media_title,
    create_manual_media_node,
    create_playlist_media_node,
    int_or_zero,
    meeting_media_from_ref,
    media_ref_title,
    meeting_media_type_from_path,
    mime_for_meeting_media,
    playlist_item_media_url,
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
    assert int_or_zero("7") == 7
    assert int_or_zero("bad") == 0


def test_create_playlist_media_node_maps_jwl_item_shape():
    raw = {
        "jworg_url": "https://example.test/video.mp4",
        "title": "  Item title  ",
        "mime_type": "video/mp4",
        "key_symbol": "mwb",
        "track": "3",
        "issue": "202406",
        "doc_id": "42",
        "auto_title": True,
    }

    node = create_playlist_media_node(
        raw,
        "Fallback",
        node_id="node-id",
        url=playlist_item_media_url(raw),
        media_fallback_title="Media",
    )

    assert node == {
        "id": "node-id",
        "type": "media",
        "title": "Item title",
        "media_type": "video",
        "media_ref": {
            "multimedia_id": 0,
            "mime_type": "video/mp4",
            "file_path": "https://example.test/video.mp4",
            "label": "Item title",
            "caption": "",
            "begin_ordinal": 0,
            "key_symbol": "mwb",
            "track": 3,
            "issue_tag": 202406,
            "meps_doc_id": 42,
            "section": "",
            "is_song": False,
            "cbs_article_title": "",
        },
        "children": [],
        "meeting_generated": False,
        "auto_title": True,
    }


def test_create_playlist_media_node_uses_fallback_title_and_media_label():
    node = create_playlist_media_node(
        {"url": "C:/meeting/photo.png"},
        "",
        node_id="node-id",
        url="C:/meeting/photo.png",
        media_fallback_title="Media",
    )

    assert node["title"] == "photo"
    assert node["media_type"] == "image"
    assert node["media_ref"]["label"] == "photo"

    empty_url_node = create_playlist_media_node(
        {},
        "",
        node_id="empty-id",
        url="",
        media_fallback_title="Media",
    )
    assert empty_url_node["title"] == "Media"
    assert empty_url_node["media_ref"]["label"] == ""


def test_meeting_media_from_ref_applies_typed_defaults():
    media = meeting_media_from_ref(
        {
            "multimedia_id": "9",
            "mime_type": "audio/mpeg",
            "file_path": "song.mp3",
            "label": "Song",
            "track": "12",
            "issue_tag": "2024",
            "meps_doc_id": "77",
            "is_song": 1,
        }
    )

    assert media.multimedia_id == 9
    assert media.mime_type == "audio/mpeg"
    assert media.file_path == "song.mp3"
    assert media.label == "Song"
    assert media.track == 12
    assert media.issue_tag == 2024
    assert media.meps_doc_id == 77
    assert media.is_song is True
    assert media.caption == ""
