from app.widgets.playlist.items import (
    _media_type_from_url,
    _new_item,
)


def test_media_type_from_url_uses_extension_groups():
    assert _media_type_from_url("cover.jpg") == "image"
    assert _media_type_from_url("song.mp3?download=1") == "audio"
    assert _media_type_from_url("clip.mp4") == "video"
    assert _media_type_from_url("") == "video"


def test_new_item_extracts_local_jw_filename_metadata():
    item = _new_item("rr_E_12", "C:/media/rr_E_12.mp4")

    assert item["key_symbol"] == "rr"
    assert item["track"] == 12
    assert item["auto_title"] is True
