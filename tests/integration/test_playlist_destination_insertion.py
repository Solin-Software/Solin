import copy
from types import SimpleNamespace

from solin.core.media.destinations import MediaDestinationOutcome, PlaylistDestinationTarget
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


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot):
        self._slots.remove(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


class _Snapshots:
    def __init__(self):
        self.writeCompleted = _Signal()
        self.writeFailed = _Signal()
        self.requests = []
        self._generations = {}

    def request(self, key, writer, *, conflict_key=None):
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        self.requests.append((key, generation, writer, conflict_key))
        return generation

    def complete_latest(self):
        key, generation, writer, _conflict_key = self.requests[-1]
        writer()
        self.writeCompleted.emit(key, generation)

    def fail_latest(self, message="disk full"):
        key, generation, _writer, _conflict_key = self.requests[-1]
        self.writeFailed.emit(key, generation, message)


class _Repository:
    def __init__(self, path):
        self.path = path
        self.saved = []

    def save_strict(self, playlists):
        self.saved.append(copy.deepcopy(playlists))


class _Selector:
    def __init__(self, action):
        self._action = action
        self.calls = []

    def select(self, paths, **options):
        self.calls.append((list(paths), options))
        self._action(options)


def _selection_widget(tmp_path, playlists, selector, snapshots):
    repository = _Repository(tmp_path / "playlists.json")
    errors = []
    warnings = []
    widget = SimpleNamespace(
        _playlists=playlists,
        _jwpub_destination_generation=0,
        _jwpub_destination_scopes=set(),
        _jwpub_selection_factory=selector,
        _playlist_repository=repository,
        _media_tree_runtime=SimpleNamespace(
            snapshots=snapshots,
            operations=SimpleNamespace(cancel_scope=lambda _scope_id: None),
        ),
        _profile_paths=SimpleNamespace(images_dir=tmp_path / "images"),
        _notifications=SimpleNamespace(
            error=errors.append,
            warning=warnings.append,
        ),
        _list_view=None,
        _edit_view=None,
        _stack=SimpleNamespace(currentIndex=lambda: 0),
        lang=SimpleNamespace(api_code="T", media_api_code="T"),
        tr=lambda text: text,
    )
    return widget, repository, errors, warnings


def test_publication_selector_uses_shared_placement_and_waits_for_persist_ack(tmp_path):
    playlist = _playlist()
    snapshots = _Snapshots()
    completed = []

    def select(options):
        assert options["list_id"] is None
        placement = options["placement_ref"]()
        assert placement["id"] == "saved"
        placement["name"] = "Mutated copy"
        options["insert"](
            [{"id": "new", "title": "New", "url": "new.mp4", "type": "video"}],
            "section:talk",
            0,
            completed.append,
        )
        assert completed == []
        snapshots.complete_latest()
        assert len(completed) == 1
        assert completed[0].target_valid is True
        assert completed[0].added_count == 1

    selector = _Selector(select)
    widget, repository, errors, _warnings = _selection_widget(
        tmp_path, [playlist], selector, snapshots
    )

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("saved", "Saved"),
    )

    assert outcome is not None
    assert outcome.added_count == 1
    assert outcome.referenced_urls == ("new.mp4",)
    assert outcome.destination_accepted is True
    assert playlist["name"] == "Saved"
    assert [item["id"] for item in playlist["items"]] == ["new", "existing"]
    assert repository.saved[-1][0]["items"][0]["id"] == "new"
    assert errors == []


def test_publication_selector_accepts_newer_persisted_generation(tmp_path):
    playlist = _playlist()
    snapshots = _Snapshots()
    completed = []

    def select(options):
        options["insert"](
            [{"id": "new", "url": "new.mp4", "type": "video"}],
            "root",
            0,
            completed.append,
        )
        assert completed == []
        key = snapshots.requests[-1][0]
        snapshots.request(key, lambda: None, conflict_key="same-playlist-file")
        snapshots.complete_latest()
        assert len(completed) == 1
        assert completed[0].target_valid is True
        assert completed[0].added_count == 1

    selector = _Selector(select)
    widget, repository, errors, _warnings = _selection_widget(
        tmp_path, [playlist], selector, snapshots
    )

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("saved", "Saved"),
    )

    assert outcome == MediaDestinationOutcome(1, ("new.mp4",))
    assert snapshots.requests[0][0] == f"playlists:{repository.path}"
    assert errors == []


def test_publication_selector_waits_for_durable_rollback_after_context_retire(tmp_path):
    playlist = _playlist()
    snapshots = _Snapshots()
    completed = []
    widget_holder = {}

    def select(options):
        options["insert"](
            [{"id": "new", "url": "new.mp4", "type": "video"}],
            "root",
            0,
            completed.append,
        )
        widget_holder["widget"]._jwpub_destination_generation += 1
        snapshots.complete_latest()
        assert completed == []
        assert [item["id"] for item in playlist["items"]] == ["existing"]
        assert len(snapshots.requests) == 2
        snapshots.complete_latest()
        assert len(completed) == 1
        assert completed[0].target_valid is False

    selector = _Selector(select)
    widget, repository, errors, _warnings = _selection_widget(
        tmp_path, [playlist], selector, snapshots
    )
    widget_holder["widget"] = widget

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("saved", "Saved"),
    )

    assert outcome is None
    assert repository.saved[0][0]["items"][0]["id"] == "new"
    assert repository.saved[-1][0]["items"][0]["id"] == "existing"
    assert errors == []


def test_publication_selector_preserves_durable_commit_when_rollback_cannot_queue(tmp_path):
    playlist = _playlist()
    snapshots = _Snapshots()
    completed = []
    widget_holder = {}

    def select(options):
        options["insert"](
            [{"id": "new", "url": "new.mp4", "type": "video"}],
            "root",
            0,
            completed.append,
        )
        widget_holder["widget"]._jwpub_destination_generation += 1
        request = snapshots.request
        snapshots.request = lambda *_args, **_kwargs: 0
        try:
            snapshots.complete_latest()
        finally:
            snapshots.request = request
        assert len(completed) == 1
        assert completed[0].target_valid is True
        assert completed[0].added_count == 1

    selector = _Selector(select)
    widget, repository, errors, _warnings = _selection_widget(
        tmp_path, [playlist], selector, snapshots
    )
    widget_holder["widget"] = widget

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("saved", "Saved"),
    )

    assert outcome == MediaDestinationOutcome(1, ("new.mp4",))
    assert [item["id"] for item in playlist["items"]] == ["existing"]
    assert repository.saved[0][0]["items"][0]["id"] == "new"
    assert len(snapshots.requests) == 1
    assert errors == []


def test_publication_selector_creates_playlist_only_after_selected_batch(tmp_path):
    snapshots = _Snapshots()
    completed = []
    playlists = []

    def select(options):
        assert playlists == []
        assert options["placement_ref"]()["items"] == []
        options["insert"](
            [
                {"id": "one", "url": "one.png", "type": "image"},
                {"id": "two", "url": "two.mp4", "type": "video"},
            ],
            "root",
            2**31 - 1,
            completed.append,
        )
        assert len(playlists) == 1
        assert completed == []
        snapshots.complete_latest()
        assert len(completed) == 1
        assert completed[0].target_valid is True
        assert completed[0].added_count == 2

    selector = _Selector(select)
    widget, repository, errors, warnings = _selection_widget(
        tmp_path, playlists, selector, snapshots
    )

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("", "Imported", create_new=True),
    )

    assert outcome is not None
    assert outcome.added_count == 2
    assert outcome.referenced_urls == ("one.png", "two.mp4")
    assert [item["id"] for item in playlists[0]["items"]] == ["one", "two"]
    assert playlists[0]["name"] == "Imported"
    assert repository.saved[-1][0]["name"] == "Imported"
    assert errors == []
    assert warnings == []


def test_publication_selector_cancel_does_not_create_new_playlist(tmp_path):
    snapshots = _Snapshots()
    playlists = []
    selector = _Selector(lambda _options: None)
    widget, repository, errors, warnings = _selection_widget(
        tmp_path, playlists, selector, snapshots
    )

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("", "Imported", create_new=True),
    )

    assert outcome is None
    assert playlists == []
    assert repository.saved == []
    assert errors == []
    assert warnings == []


def test_publication_selector_rolls_back_when_snapshot_save_fails(tmp_path):
    playlist = _playlist()
    snapshots = _Snapshots()
    completed = []

    def select(options):
        options["insert"](
            [{"id": "new", "url": "new.mp4", "type": "video"}],
            "root",
            0,
            completed.append,
        )
        assert [item["id"] for item in playlist["items"]] == ["new", "existing"]
        snapshots.fail_latest()
        assert len(completed) == 1
        assert completed[0].target_valid is False

    selector = _Selector(select)
    widget, repository, errors, _warnings = _selection_widget(
        tmp_path, [playlist], selector, snapshots
    )

    outcome = PlaylistWidget.import_publications_to_playlist_target(
        widget,
        ["source.jwpub"],
        PlaylistDestinationTarget("saved", "Saved"),
    )

    assert outcome is None
    assert [item["id"] for item in playlist["items"]] == ["existing"]
    assert len(snapshots.requests) == 2
    snapshots.complete_latest()
    assert repository.saved[-1][0]["items"][0]["id"] == "existing"
    assert len(errors) == 1


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
