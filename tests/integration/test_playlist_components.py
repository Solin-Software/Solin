from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMenu

from solin.widgets.playlist.components import (
    PLAYLIST_CARD_MENU_STYLESHEET,
    playlist_card_menu_stylesheet,
)


_APP = QApplication.instance() or QApplication([])


def test_playlist_menu_style_lives_with_cards():
    assert "QMenu" in PLAYLIST_CARD_MENU_STYLESHEET
    assert "QMenu::item:selected" in PLAYLIST_CARD_MENU_STYLESHEET


def test_playlist_menu_stylesheet_is_accepted_by_qt() -> None:
    messages: list[str] = []

    def capture_message(_mode, _context, message: str) -> None:
        messages.append(message)

    previous_handler = qInstallMessageHandler(capture_message)
    try:
        menu = QMenu()
        menu.setStyleSheet(playlist_card_menu_stylesheet())
        menu.ensurePolished()
    finally:
        qInstallMessageHandler(previous_handler)

    assert not any("Could not parse stylesheet" in message for message in messages)
    menu.deleteLater()
