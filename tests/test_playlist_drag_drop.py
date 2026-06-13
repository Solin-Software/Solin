from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.drag_drop import _PlaylistDragDropMixin


def test_playlist_edit_view_uses_drag_drop_mixin():
    assert playlist_widget._PlaylistDragDropMixin is _PlaylistDragDropMixin
    assert issubclass(playlist_widget._PlaylistEditView, _PlaylistDragDropMixin)
    assert playlist_widget._PlaylistEditView.dropEvent is _PlaylistDragDropMixin.dropEvent
    assert playlist_widget._PlaylistEditView.get_drop_context is (
        _PlaylistDragDropMixin.get_drop_context
    )
