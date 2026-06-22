import copy

from solin.core.media.cache import MediaCacheManager
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.ui.qml.playlist.model import PlaylistEditModel


def _model(tmp_path) -> PlaylistEditModel:
    return PlaylistEditModel(
        MediaCacheManager(
            tmp_path / "media",
            downloader_factory=lambda _parent: None,
        ),
        ThumbnailStore(tmp_path / "thumbs"),
    )


def _nested_playlist():
    return {
        "sections": [
            {
                "id": "section-1",
                "name": "Opening",
                "color_hue": 215,
                "collapsed": False,
                "position": 0,
            },
            {
                "id": "subsection-1",
                "name": "Songs",
                "parent_id": "section-1",
                "collapsed": False,
                "position": 1,
            },
        ],
        "items": [
            {
                "id": "section-media",
                "title": "Introduction",
                "type": "video",
                "url": "",
                "section_id": "section-1",
            },
            {
                "id": "subsection-media",
                "title": "Song",
                "type": "audio",
                "url": "",
                "section_id": "subsection-1",
            },
            {
                "id": "root-media",
                "title": "Closing",
                "type": "video",
                "url": "",
                "section_id": None,
            },
        ],
        "markers": [
            {
                "id": "marker-1",
                "text": "Stand",
                "subsection_id": "subsection-1",
                "position": 1,
                "slot_order": 0,
            },
        ],
    }


def test_playlist_edit_model_builds_flat_rows_and_tree_data(tmp_path):
    model = _model(tmp_path)
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


def test_tree_data_preserves_nested_marker_and_media_order(tmp_path):
    model = _model(tmp_path)
    model.rebuild(_nested_playlist())

    tree = model.tree_data()

    assert [node["id"] for node in tree] == ["section-1", "root-media"]
    section = tree[0]
    assert section["itemCount"] == 2
    assert [node["id"] for node in section["children"]] == [
        "section-media",
        "subsection-1",
    ]
    subsection = section["children"][1]
    assert [node["id"] for node in subsection["children"]] == [
        "marker-1",
        "subsection-media",
    ]


def test_tree_data_is_cached_until_model_rebuild(tmp_path):
    playlist = _nested_playlist()
    model = _model(tmp_path)
    model.rebuild(playlist)

    first = model.tree_data()
    second = model.tree_data()
    playlist["items"][0]["title"] = "Changed after cache"

    assert second is first
    assert model.tree_data()[0]["children"][0]["title"] == "Introduction"

    model.rebuild(playlist)

    assert model.tree_data() is not first
    assert model.tree_data()[0]["children"][0]["title"] == "Changed after cache"


def test_section_patch_updates_cached_rows_without_full_rebuild(tmp_path):
    playlist = _nested_playlist()
    model = _model(tmp_path)
    model.rebuild(playlist)
    first_tree = model.tree_data()
    playlist["sections"][0]["name"] = "Updated opening"
    playlist["sections"][0]["color_hue"] = 120

    model.update_section("section-1")

    patch = model.section_patch("section-1")
    assert patch["title"] == "Updated opening"
    assert patch["itemCount"] == 2
    assert model.entry_at(0)["name"] == "Updated opening"
    assert model.tree_data() is not first_tree


def test_move_media_to_subsection_updates_storage_and_marker_positions(tmp_path):
    playlist = _nested_playlist()
    model = _model(tmp_path)
    model.rebuild(playlist)

    assert model.move_node("root-media", "subsection:subsection-1", 0)

    assert [item["id"] for item in playlist["items"]] == [
        "section-media",
        "root-media",
        "subsection-media",
    ]
    root_media = next(item for item in playlist["items"] if item["id"] == "root-media")
    assert root_media["section_id"] == "subsection-1"
    assert playlist["markers"][0]["position"] == 2
    subsection = model.tree_data()[0]["children"][1]
    assert [node["id"] for node in subsection["children"]] == [
        "root-media",
        "marker-1",
        "subsection-media",
    ]


def test_empty_subsection_move_persists_before_section_media(tmp_path):
    playlist = {
        "sections": [
            {
                "id": "section-1",
                "name": "Main",
                "collapsed": False,
                "position": 0,
            },
            {
                "id": "empty-subsection",
                "name": "Empty",
                "parent_id": "section-1",
                "collapsed": False,
                "position": 2,
            },
        ],
        "items": [
            {
                "id": "media-1",
                "title": "First",
                "type": "video",
                "url": "",
                "section_id": "section-1",
            },
            {
                "id": "media-2",
                "title": "Second",
                "type": "video",
                "url": "",
                "section_id": "section-1",
            },
        ],
        "markers": [],
    }
    model = _model(tmp_path)
    model.rebuild(playlist)

    assert model.move_node("empty-subsection", "section:section-1", 1)
    model.rebuild(playlist)

    section = model.tree_data()[0]
    assert [node["id"] for node in section["children"]] == [
        "media-1",
        "empty-subsection",
        "media-2",
    ]
    empty = next(
        section for section in playlist["sections"]
        if section["id"] == "empty-subsection"
    )
    assert empty["position"] == 1
    assert empty["slot_order"] == 0


def test_empty_top_level_section_move_persists_before_root_media(tmp_path):
    playlist = {
        "sections": [
            {
                "id": "empty-section",
                "name": "Empty",
                "collapsed": False,
                "position": 1,
            },
        ],
        "items": [
            {
                "id": "root-media",
                "title": "Root",
                "type": "video",
                "url": "",
                "section_id": None,
            },
        ],
        "markers": [],
    }
    model = _model(tmp_path)
    model.rebuild(playlist)

    assert model.move_node("empty-section", "root", 0)
    model.rebuild(playlist)

    assert [node["id"] for node in model.tree_data()] == [
        "empty-section",
        "root-media",
    ]
    assert playlist["sections"][0]["position"] == 0
    assert playlist["sections"][0]["slot_order"] == 0


def test_invalid_section_drop_into_descendant_preserves_storage(tmp_path):
    playlist = _nested_playlist()
    before = copy.deepcopy(playlist)
    model = _model(tmp_path)
    model.rebuild(playlist)

    assert not model.move_node("section-1", "subsection:subsection-1", 0)

    assert playlist == before


def test_marker_can_only_move_inside_its_own_subsection(tmp_path):
    playlist = _nested_playlist()
    playlist["sections"].extend([
        {
            "id": "section-2",
            "name": "Second",
            "color_hue": 120,
            "collapsed": False,
            "position": 3,
        },
        {
            "id": "subsection-2",
            "name": "Other",
            "parent_id": "section-2",
            "collapsed": False,
            "position": 3,
        },
    ])
    model = _model(tmp_path)
    model.rebuild(playlist)

    assert model.can_drop_node("marker-1", "marker", "subsection:subsection-1")
    assert not model.can_drop_node(
        "marker-1",
        "marker",
        "subsection:subsection-2",
    )
    assert not model.can_drop_node("marker-1", "marker", "section:section-1")


def test_rebuild_repairs_orphaned_item_section_reference(tmp_path):
    playlist = {
        "sections": [],
        "items": [
            {
                "id": "orphan",
                "title": "Orphan",
                "type": "video",
                "url": "",
                "section_id": "missing-section",
            },
        ],
        "markers": [],
    }
    model = _model(tmp_path)

    model.rebuild(playlist)

    assert playlist["items"][0]["section_id"] is None
    assert model.tree_data()[0]["id"] == "orphan"
