from app.widgets.playlist.list_view import _BTN_PRIMARY, _BTN_STYLE


def test_playlist_list_view_styles_define_primary_and_secondary_buttons():
    assert "QPushButton" in _BTN_STYLE
    assert "#388bfd" in _BTN_PRIMARY
