from app.widgets.playlist.edit_model import PlaylistEditModel


def test_playlist_edit_model_builds_flat_rows_and_tree_data():
    model = PlaylistEditModel()
    model.rebuild(
        {
            "sections": [
                {
                    "id": "section-1",
                    "name": "Opening",
                    "color_hue": 215,
                    "collapsed": False,
                }
            ],
            "items": [
                {
                    "id": "media-1",
                    "title": "Song",
                    "type": "audio",
                    "url": "",
                    "section_id": "section-1",
                    "base_duration_ticks": 65 * 10_000_000,
                }
            ],
            "markers": [],
        }
    )

    assert model.rowCount() == 2
    assert model.roleNames()[PlaylistEditModel.EntryTypeRole] == b"entryType"
    assert model.data(model.index(0), PlaylistEditModel.EntryTypeRole) == "section"
    assert model.data(model.index(1), PlaylistEditModel.TitleRole) == "Song"
    assert model.data(model.index(1), PlaylistEditModel.DurationTextRole) == "1:05"

    tree = model.tree_data()
    assert tree[0]["type"] == "section"
    assert tree[0]["children"][0]["id"] == "media-1"
