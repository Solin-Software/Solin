from types import SimpleNamespace

from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.edit_actions import PlaylistEditActionsMixin


def test_playlist_edit_view_uses_actions_mixin():
    assert playlist_widget.PlaylistEditActionsMixin is PlaylistEditActionsMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistEditActionsMixin)
    assert playlist_widget.PlaylistEditView._add_files is PlaylistEditActionsMixin._add_files
    assert playlist_widget.PlaylistEditView.load_temp_playlist is (
        PlaylistEditActionsMixin.load_temp_playlist
    )


def test_temp_playlist_sessions_receive_unique_ids():
    loaded = []
    view = SimpleNamespace(
        lang=None,
        tr=lambda text: text,
        load_playlist=loaded.append,
    )

    first = PlaylistEditActionsMixin.load_temp_playlist(view, [])
    second = PlaylistEditActionsMixin.load_temp_playlist(view, [])

    assert first != second
    assert loaded[0]["id"] == first
    assert loaded[1]["id"] == second


def test_append_temp_playlist_items_rejects_stale_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _rebuild_list=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "stale",
        [{"title": "Late result"}],
    )

    assert appended is False
    assert edit_view._pl["items"] == []
    assert rebuilds == []


def test_append_temp_playlist_items_updates_matching_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _rebuild_list=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)
    item = {"title": "Imported"}

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "current",
        [item],
    )
    item["title"] = "Mutated externally"

    assert appended is True
    assert edit_view._pl["items"] == [{"title": "Imported"}]
    assert rebuilds == [True]
