import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.playlist import ProjectionPlaylistMixin


def test_projection_bar_uses_playlist_mixin():
    assert projection_bar.ProjectionPlaylistMixin is ProjectionPlaylistMixin
    assert issubclass(projection_bar.ProjectionBar, ProjectionPlaylistMixin)
    assert projection_bar.ProjectionBar.set_playlist is ProjectionPlaylistMixin.set_playlist
    assert (
        projection_bar.ProjectionBar._toggle_playlist_panel
        is ProjectionPlaylistMixin._toggle_playlist_panel
    )
    assert (
        projection_bar.ProjectionBar._resolve_idle_path
        is ProjectionPlaylistMixin._resolve_idle_path
    )
