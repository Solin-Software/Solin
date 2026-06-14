from solin.core.media.formats import media_type_from_path
from solin.core.playlists.items import (
    create_playlist_item,
    looks_like_filename_title,
)


def test_media_type_from_path_uses_extension_groups():
    assert media_type_from_path("cover.jpg", default="video") == "image"
    assert media_type_from_path("song.mp3?download=1", default="video") == "audio"
    assert media_type_from_path("clip.mp4", default="video") == "video"
    assert media_type_from_path("", default="video") == "video"


def test_create_playlist_item_extracts_local_jw_filename_metadata():
    item = create_playlist_item("rr_E_12", "C:/media/rr_E_12.mp4")

    assert item["key_symbol"] == "rr"
    assert item["track"] == 12
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


def test_create_playlist_item_uses_original_filename_for_jw_reference():
    item = create_playlist_item(
        "Song",
        "C:/cache/opaque-id.mp3",
        original_filename="rr_T_43.mp3",
    )

    assert item["key_symbol"] == "rr"
    assert item["track"] == 43
    assert item["meps_language"] == 5


def test_looks_like_filename_title_only_accepts_audio_or_video_names():
    assert looks_like_filename_title("") is True
    assert looks_like_filename_title("clip.mp4") is True
    assert looks_like_filename_title("cover.jpg") is False
    assert looks_like_filename_title("Opening Song") is False
