from app.widgets.playlist import widget as playlist_widget
from app.widgets.playlist.import_export import _PlaylistEditImportMixin


def test_playlist_edit_view_uses_import_export_mixin():
    assert playlist_widget._PlaylistEditImportMixin is _PlaylistEditImportMixin
    assert issubclass(playlist_widget._PlaylistEditView, _PlaylistEditImportMixin)
    assert playlist_widget._PlaylistEditView._import_pdfs is _PlaylistEditImportMixin._import_pdfs
    assert playlist_widget._PlaylistEditView._import_jwlplaylists_drop is (
        _PlaylistEditImportMixin._import_jwlplaylists_drop
    )
