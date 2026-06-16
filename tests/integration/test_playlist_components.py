from solin.widgets.playlist.components import PLAYLIST_CARD_MENU_STYLESHEET


def test_playlist_menu_style_lives_with_cards():
    assert "QMenu" in PLAYLIST_CARD_MENU_STYLESHEET
    assert "QMenu::item:selected" in PLAYLIST_CARD_MENU_STYLESHEET
