from types import SimpleNamespace

from solin.core.media.cache import MediaCacheManager
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.ui.qml.playlist.model import PlaylistEditModel
from solin.widgets.playlist.widget import PlaylistWidget


def _model(tmp_path):
    return PlaylistEditModel(
        MediaCacheManager(
            tmp_path / "media",
            downloader_factory=lambda _parent: None,
        ),
        ThumbnailStore(tmp_path / "thumbs"),
    )


def _playlist():
    return {
        "id": "saved",
        "name": "Saved",
        "sections": [
            {"id": "talk", "name": "Talk", "position": 0, "color_hue": 215}
        ],
        "items": [
            {
                "id": "existing",
                "title": "Existing",
                "url": "existing.mp4",
                "type": "video",
                "section_id": "talk",
            }
        ],
        "markers": [],
    }


def _widget(tmp_path, playlist):
    saves = []
    widget = SimpleNamespace(
        _playlists=[playlist],
        _edit_view=SimpleNamespace(
            model=_model(tmp_path),
            _pl=None,
            _rebuild_list=lambda: None,
        ),
        _playlist_repository=SimpleNamespace(
            save=lambda playlists: saves.append(playlists)
        ),
        _stack=SimpleNamespace(currentIndex=lambda: 0),
    )
    return widget, saves


def test_playlist_destination_inserts_into_section_and_saves_once(tmp_path):
    playlist = _playlist()
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [
            {
                "id": "new",
                "title": "New",
                "url": "new.mp4",
                "type": "video",
            }
        ],
        list_id="section:talk",
        insert_index=0,
    )

    assert [item["id"] for item in playlist["items"]] == ["new", "existing"]
    assert result.target_valid is True
    assert len(result.added_items) == 1
    assert len(saves) == 1


def test_playlist_destination_rejects_stale_section_and_does_not_save(tmp_path):
    playlist = _playlist()
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [
            {
                "id": "new",
                "title": "New",
                "url": "new.mp4",
                "type": "video",
            }
        ],
        list_id="section:removed",
        insert_index=0,
    )

    assert result.target_valid is False
    assert [item["id"] for item in playlist["items"]] == ["existing"]
    assert saves == []


def test_playlist_placement_ref_is_an_isolated_snapshot(tmp_path):
    playlist = _playlist()
    widget, _saves = _widget(tmp_path, playlist)

    snapshot = PlaylistWidget.playlist_placement_ref(widget, "saved")
    assert snapshot is not None
    snapshot["sections"][0]["name"] = "Changed"

    assert playlist["sections"][0]["name"] == "Talk"


def test_playlist_destination_duplicate_does_not_save_again(tmp_path):
    playlist = _playlist()
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [
            {
                "id": "duplicate",
                "title": "Existing",
                "url": "existing.mp4",
                "type": "video",
            }
        ],
        list_id="root",
        insert_index=0,
    )

    assert result.duplicate_count == 1
    assert result.added_items == ()
    assert saves == []


def test_playlist_destination_uses_canonical_jw_identity(tmp_path):
    playlist = _playlist()
    playlist["items"][0].update(
        {
            "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r480P.mp4",
            "key_symbol": "sjjm",
            "track": 2,
            "meps_language": 5,
        }
    )
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [
            {
                "id": "duplicate",
                "title": "Same song",
                "url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r720P.mp4",
                "key_symbol": "sjjm",
                "track": 2,
                "meps_language": 5,
            }
        ],
        list_id="root",
        insert_index=0,
    )

    assert result.duplicate_count == 1
    assert [item["id"] for item in playlist["items"]] == ["existing"]
    assert saves == []


def test_playlist_destination_partitions_partial_duplicate_batch(tmp_path):
    playlist = _playlist()
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [
            {"id": "old", "title": "Old", "url": "existing.mp4"},
            {"id": "first", "title": "First", "url": "first.mp4"},
            {"id": "repeat", "title": "Repeat", "url": "first.mp4"},
            {"id": "second", "title": "Second", "url": "second.mp4"},
        ],
        list_id="root",
        insert_index=0,
    )

    assert result.added_count == 2
    assert result.duplicate_count == 2
    assert [item["id"] for item in playlist["items"]] == [
        "first",
        "second",
        "existing",
    ]
    assert len(saves) == 1
