from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.drag_drop import PlaylistDragDropMixin


def test_playlist_edit_view_uses_drag_drop_mixin():
    assert playlist_widget.PlaylistDragDropMixin is PlaylistDragDropMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistDragDropMixin)
    assert playlist_widget.PlaylistEditView.dropEvent is PlaylistDragDropMixin.dropEvent
    assert playlist_widget.PlaylistEditView.get_drop_context is (
        PlaylistDragDropMixin.get_drop_context
    )
