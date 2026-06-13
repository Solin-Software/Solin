from solin.widgets.playlist.items import (
    media_type_from_url,
    new_playlist_item,
)


def test_media_type_from_url_uses_extension_groups():
    assert media_type_from_url("cover.jpg") == "image"
    assert media_type_from_url("song.mp3?download=1") == "audio"
    assert media_type_from_url("clip.mp4") == "video"
    assert media_type_from_url("") == "video"


def test_new_playlist_item_extracts_local_jw_filename_metadata():
    item = new_playlist_item("rr_E_12", "C:/media/rr_E_12.mp4")

    assert item["key_symbol"] == "rr"
    assert item["track"] == 12
    assert item["auto_title"] is True
