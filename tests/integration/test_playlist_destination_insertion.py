from types import SimpleNamespace

from solin.widgets.playlist.widget import PlaylistWidget


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
            _pl=None,
            _reconcile_playlist=lambda: None,
        ),
        _persist_playlists=lambda: saves.append(playlist),
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


def test_playlist_destination_inserts_before_deferred_ui_is_built(tmp_path):
    playlist = _playlist()
    saves = []
    widget = SimpleNamespace(
        _playlists=[playlist],
        _edit_view=None,
        _persist_playlists=lambda: saves.append(playlist),
    )

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
        list_id="root",
        insert_index=0,
    )

    assert result.target_valid is True
    assert [item["id"] for item in playlist["items"]] == ["new", "existing"]
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


def test_playlist_destination_adds_repeated_video_as_new_occurrence(tmp_path):
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

    assert result.duplicate_count == 0
    assert [item["id"] for item in result.added_items] == ["duplicate"]
    assert [item["id"] for item in playlist["items"]] == ["duplicate", "existing"]
    assert len(saves) == 1


def test_playlist_destination_allows_repeated_canonical_jw_video(tmp_path):
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

    assert result.duplicate_count == 0
    assert [item["id"] for item in playlist["items"]] == ["duplicate", "existing"]
    assert len(saves) == 1


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

    assert result.added_count == 4
    assert result.duplicate_count == 0
    assert [item["id"] for item in playlist["items"]] == [
        "old",
        "first",
        "repeat",
        "second",
        "existing",
    ]
    assert len(saves) == 1


def test_playlist_destination_still_rejects_repeated_audio(tmp_path):
    playlist = _playlist()
    playlist["items"] = [
        {"id": "existing-audio", "url": "song.mp3", "type": "audio"}
    ]
    widget, saves = _widget(tmp_path, playlist)

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        [{"id": "duplicate", "url": "song.mp3", "type": "audio"}],
        list_id="root",
        insert_index=0,
    )

    assert result.added_items == ()
    assert result.duplicate_count == 1
    assert saves == []


def test_playlist_destination_regenerates_colliding_occurrence_ids(tmp_path):
    playlist = _playlist()
    widget, saves = _widget(tmp_path, playlist)
    candidates = [
        {
            "id": "existing",
            "url": "existing.mp4",
            "type": "video",
            "start_trim_ticks": 10,
        },
        {
            "id": "existing",
            "url": "existing.mp4",
            "type": "video",
            "start_trim_ticks": 20,
        },
    ]

    result = PlaylistWidget.add_items_to_playlist(
        widget,
        "saved",
        candidates,
        list_id="root",
        insert_index=0,
    )

    assert result.added_count == 2
    assert len({item["id"] for item in playlist["items"]}) == 3
    assert [item["start_trim_ticks"] for item in result.added_items] == [10, 20]
    assert candidates[0]["id"] == candidates[1]["id"] == "existing"
    assert len(saves) == 1
