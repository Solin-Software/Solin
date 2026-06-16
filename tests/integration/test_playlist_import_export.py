from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.import_export import PlaylistEditImportMixin


def test_playlist_edit_view_uses_import_export_mixin():
    assert playlist_widget.PlaylistEditImportMixin is PlaylistEditImportMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistEditImportMixin)
    assert playlist_widget.PlaylistEditView._import_pdfs is PlaylistEditImportMixin._import_pdfs
    assert playlist_widget.PlaylistEditView._import_jwlplaylists_drop is (
        PlaylistEditImportMixin._import_jwlplaylists_drop
    )
