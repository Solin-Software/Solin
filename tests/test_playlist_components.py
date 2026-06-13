from solin.widgets.playlist.components import _MENU_STYLE


def test_playlist_menu_style_lives_with_cards():
    assert "QMenu" in _MENU_STYLE
    assert "QMenu::item:selected" in _MENU_STYLE
