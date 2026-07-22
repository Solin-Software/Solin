from solin.core.media.formats import media_type_from_path
from solin.core.playlists.items import (
    create_playlist_item,
    looks_like_filename_title,
    playlist_items_from_jwpub,
)


def test_media_type_from_path_uses_extension_groups():
    assert media_type_from_path("cover.jpg", default="video") == "image"
    assert media_type_from_path("song.mp3?download=1", default="video") == "audio"
    assert media_type_from_path("clip.mp4", default="video") == "video"
    assert media_type_from_path("", default="video") == "video"


def test_create_playlist_item_does_not_promote_local_filename_to_jw_identity():
    item = create_playlist_item("rr_E_12", "C:/media/rr_E_12.mp4")

    assert item["key_symbol"] is None
    assert item["track"] is None
    assert item["auto_title"] is True


def test_create_playlist_item_preserves_extra_persisted_attributes():
    item = create_playlist_item(
        "Welcome",
        "C:/media/welcome.mp4",
        id="stable-id",
        section_id="section-1",
    )

    assert item["id"] == "stable-id"
    assert item["section_id"] == "section-1"


def test_create_playlist_item_does_not_promote_untrusted_original_filename():
    item = create_playlist_item(
        "Song",
        "C:/cache/opaque-id.mp3",
        original_filename="rr_T_43.mp3",
    )

    assert item["key_symbol"] is None
    assert item["track"] is None
    assert item["meps_language"] == 0


def test_create_playlist_item_marks_explicit_local_jw_reference_authoritative():
    item = create_playlist_item(
        "Song",
        "C:/cache/opaque-id.mp3",
        key_symbol="rr",
        track=43,
        meps_language=5,
    )

    assert item["jw_identity_authoritative"] is True


def test_looks_like_filename_title_only_accepts_audio_or_video_names():
    assert looks_like_filename_title("") is True
    assert looks_like_filename_title("clip.mp4") is True
    assert looks_like_filename_title("cover.jpg") is False
    assert looks_like_filename_title("Opening Song") is False


def test_playlist_items_from_jwpub_preserves_metadata_and_manual_title():
    items = playlist_items_from_jwpub(
        [{
            "title": "Imported title",
            "url": "https://example.test/media.mp4",
            "type": "video",
            "key_symbol": "lff",
            "track": 3,
            "meps_language": 5,
        }],
        "Fallback",
    )

    assert len(items) == 1
    assert items[0]["title"] == "Imported title"
    assert items[0]["key_symbol"] == "lff"
    assert items[0]["track"] == 3
    assert items[0]["meps_language"] == 5
    assert items[0]["auto_title"] is False
