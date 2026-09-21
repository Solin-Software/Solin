from solin.widgets.playlist.list_view import PLAYLIST_PRIMARY_BUTTON_STYLESHEET, PLAYLIST_SECONDARY_BUTTON_STYLESHEET

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QMessageBox

from solin.widgets.playlist.list_view import PlaylistListView


def test_playlist_list_view_styles_define_primary_and_secondary_buttons():
    assert "QPushButton" in PLAYLIST_SECONDARY_BUTTON_STYLESHEET
    assert "#388bfd" in PLAYLIST_PRIMARY_BUTTON_STYLESHEET


@pytest.mark.parametrize("confirmed", [False, True])
def test_linked_sync_reset_requires_confirmation_and_uses_worker(monkeypatch, confirmed):
    store = SimpleNamespace(reset_sync=Mock())
    submit = Mock()
    view = SimpleNamespace(
        tr=lambda text: text,
        _watched_folder_playlist_store=store,
        _submit_watched_folder_mutation=submit,
    )
    question = Mock(return_value=(QMessageBox.StandardButton.Yes if confirmed
                                 else QMessageBox.StandardButton.No))
    monkeypatch.setattr(QMessageBox, "question", question)
    PlaylistListView._reset_watched_folder_sync(view, "/linked/Playlist")
    assert question.call_args.args[-1] == QMessageBox.StandardButton.No
    store.reset_sync.assert_not_called()
    if confirmed:
        path, operation, callback = submit.call_args.args
        assert (path, operation) == ("/linked/Playlist", "reset_linked_playlist_sync")
        callback()
        store.reset_sync.assert_called_once_with(path)
    else:
        submit.assert_not_called()
