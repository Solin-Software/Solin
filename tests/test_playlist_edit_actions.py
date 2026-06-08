from app.widgets.playlist import widget as playlist_widget
from app.widgets.playlist.edit_actions import _PlaylistEditActionsMixin


def test_playlist_edit_view_uses_actions_mixin():
    assert playlist_widget._PlaylistEditActionsMixin is _PlaylistEditActionsMixin
    assert issubclass(playlist_widget._PlaylistEditView, _PlaylistEditActionsMixin)
    assert playlist_widget._PlaylistEditView._add_files is _PlaylistEditActionsMixin._add_files
    assert playlist_widget._PlaylistEditView.load_temp_playlist is (
        _PlaylistEditActionsMixin.load_temp_playlist
    )
