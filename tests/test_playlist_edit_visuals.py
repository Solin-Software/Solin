from solin.core.meetings.colors import hsl_to_hex
from solin.ui.qml.playlist.visuals import (
    PLAYLIST_ICON_SVGS,
    UNSECTIONED_CARD_BACKGROUND,
    PlaylistIconProvider,
    PlaylistThumbnailProvider,
    playlist_media_badge,
    round_playlist_pixmap,
)


def test_playlist_edit_visual_providers_are_available():
    assert PlaylistThumbnailProvider is not None
    assert PlaylistIconProvider is not None
    assert round_playlist_pixmap is not None


def test_playlist_edit_visual_helpers_stay_stable():
    assert hsl_to_hex(0, 100, 50) == "#ff0000"
    assert playlist_media_badge("image") == "Image"
    assert playlist_media_badge("audio") == "Audio"
    assert playlist_media_badge("video") == "Video"
    assert UNSECTIONED_CARD_BACKGROUND == "transparent"


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

    assert set(PLAYLIST_ICON_SVGS) == expected
    assert all(svg.startswith("<svg") for svg in PLAYLIST_ICON_SVGS.values())
