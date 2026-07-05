import solin.widgets.projection.bar as projection_bar
from solin.widgets.projection.playlist import ProjectionPlaylistMixin


class _Signal:
    def __init__(self):
        self.emissions = []

    def emit(self, *args):
        self.emissions.append(args)


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


def test_source_duration_is_published_without_reaching_into_playlist_widgets():
    signal = _Signal()
    host = type("ProjectionHost", (), {})()
    host.current_playlist_item = lambda: {"id": "media-1"}
    host.source_duration_discovered = signal

    projection_bar.ProjectionBar._on_source_duration_changed(host, 12_345)

    assert signal.emissions == [("media-1", 12_345)]


def test_source_duration_ignores_missing_items_and_invalid_values():
    signal = _Signal()
    host = type("ProjectionHost", (), {})()
    host.current_playlist_item = lambda: None
    host.source_duration_discovered = signal

    projection_bar.ProjectionBar._on_source_duration_changed(host, 12_345)
    projection_bar.ProjectionBar._on_source_duration_changed(host, 0)

    assert signal.emissions == []
