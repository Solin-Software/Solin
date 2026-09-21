from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMenu

from solin.widgets.playlist.components import (
    PLAYLIST_CARD_MENU_STYLESHEET,
    playlist_card_menu_stylesheet,
    WatchedFolderCard,
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


def test_watched_folder_menu_exposes_sync_reset(monkeypatch):
    from solin.widgets.playlist import components

    card = WatchedFolderCard("/linked/Playlist", "Playlist", 1, None)
    requests = []
    card.reset_sync_req.connect(requests.append)

    class InspectMenu(QMenu):
        def exec(self, *_args):
            action = next(action for action in self.actions() if action.text() == "Reset synchronization")
            action.trigger()

    monkeypatch.setattr(components, "QMenu", InspectMenu)
    card._show_menu()
    assert requests == ["/linked/Playlist"]
    card.deleteLater()
