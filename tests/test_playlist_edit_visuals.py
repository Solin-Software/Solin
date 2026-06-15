from solin.core.meetings.colors import _hsl_to_hex
from solin.ui.qml.playlist.visuals import (
    PlaylistIconProvider,
    PlaylistThumbnailProvider,
    _ICON_MAP,
    _UNSECTIONED_BG,
    _media_badge,
    _round_pixmap,
)


def test_playlist_edit_visual_providers_are_available():
    assert PlaylistThumbnailProvider is not None
    assert PlaylistIconProvider is not None
    assert _round_pixmap is not None


def test_playlist_edit_visual_helpers_stay_stable():
    assert _hsl_to_hex(0, 100, 50) == "#ff0000"
    assert _media_badge("image") == "Image"
    assert _media_badge("audio") == "Audio"
    assert _media_badge("video") == "Video"
    assert _UNSECTIONED_BG == "transparent"


def test_playlist_icons_stay_embedded_in_python():
    expected = {
        "grip",
        "more",
        "play_all",
        "shuffle",
        "back",
        "plus",
        "export",
        "folder_link",
        "save",
        "cloud",
        "section",
        "marker",
        "chevron_down",
        "chevron_up",
        "edit",
        "trash",
        "palette",
        "media_image",
        "media_audio",
        "media_video",
    }

    assert set(_ICON_MAP) == expected
    assert all(svg.startswith("<svg") for svg in _ICON_MAP.values())
