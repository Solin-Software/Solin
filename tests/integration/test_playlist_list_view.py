from solin.widgets.playlist.list_view import PLAYLIST_PRIMARY_BUTTON_STYLESHEET, PLAYLIST_SECONDARY_BUTTON_STYLESHEET


def test_playlist_list_view_styles_define_primary_and_secondary_buttons():
    assert "QPushButton" in PLAYLIST_SECONDARY_BUTTON_STYLESHEET
    assert "#388bfd" in PLAYLIST_PRIMARY_BUTTON_STYLESHEET
