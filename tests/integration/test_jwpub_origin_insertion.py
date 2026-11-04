"""Origin adapters preserve selection, placement, ownership and commit acknowledgements."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.media.placement import END_OF_LIST_INDEX
from solin.core.playlists.tree_editing import insert_playlist_media
from solin.widgets.meetings.tree_controller import MeetingTreeController
from solin.widgets.playlist.import_export import PlaylistEditImportMixin


class _Selector:
    def __init__(self):
        self.calls = []

    def select(self, paths, **options):
        self.calls.append((paths, options))


class _Operations:
    def __init__(self):
        self.specs = []

    def submit(self, spec):
        self.specs.append(spec)
        return True


def _meeting():
    selector = _Selector()
    pending = []
    controller = SimpleNamespace(
        _tree_session=SimpleNamespace(owner_id="meeting:test", generation=1),
        _tree_key="test",
        _language_code="T",
        _meeting_type="wt",
        _folder_mutation_pending=False,
        _jwpub_origin_generation=0,
        _jwpub_selection_factory=selector,
        _media_tree_runtime=SimpleNamespace(operations=_Operations()),
        _jwpub_image_dir=lambda: "images",
        _node_from_playlist_item=lambda item, title: {"type": "media", "title": title, **item},
        _insert_nodes=lambda *args, **kwargs: pending.append((args, kwargs)) or True,
        placement_playlist_ref=lambda: {"items": [], "sections": []},
        parent=lambda: None,
        _nodes=[{
            "id": "actual-id", "type": "section", "title": "Renamed talk",
            "meeting_source_key": "section:wt:public_talk", "children": [],
        }],
    )
    controller._public_talk_target = lambda: MeetingTreeController._public_talk_target(controller)
    return controller, selector, pending


@pytest.mark.parametrize("meeting_type", ["wt", "mwb", "memorial"])
def test_public_talk_auto_target_uses_current_semantic_section(meeting_type):
    controller, _, _ = _meeting()
    controller._meeting_type = meeting_type
    expected = ("section:actual-id", END_OF_LIST_INDEX) if meeting_type == "wt" else None
    assert controller._public_talk_target() == expected
    controller._nodes[0]["meeting_source_key"] = "manual"
    controller._nodes[0]["title"] = "Public talk"
    assert controller._public_talk_target() is None
    controller._nodes.clear()
    assert controller._public_talk_target() is None


@pytest.mark.parametrize("list_id", [None, "section:actual-id"])
def test_meeting_selection_waits_for_insertion_and_acknowledges_once(list_id):
    controller, selector, pending = _meeting()
    MeetingTreeController._import_jwpubs(controller, ["one.jwpub", "two.jwpub"], list_id, 4)
    paths, options = selector.calls[0]
    assert paths == ["one.jwpub", "two.jwpub"]
    assert options["list_id"] == list_id
    assert options["insert_index"] == 4
    assert options["auto_target"] is None
    outcomes = []
    options["insert"]([{"title": "Selected", "meps_language": 5}], "root", 4, outcomes.append)
    assert outcomes == []
    args, kwargs = pending[0]
    assert args[2][0]["meps_language"] == 5
    kwargs["on_completed"]([])
    kwargs["on_completed"](None)
    assert outcomes == [True]


@pytest.mark.parametrize("expiry", ["generation", "origin", "language"])
def test_meeting_selection_rejects_retired_origin(expiry):
    controller, selector, pending = _meeting()
    MeetingTreeController._import_jwpubs(controller, ["one.jwpub"])
    options = selector.calls[0][1]
    if expiry == "generation":
        controller._tree_session.generation += 1
    elif expiry == "origin":
        controller._jwpub_origin_generation += 1
    else:
        controller._language_code = "E"
    outcomes = []
    options["insert"]([{"title": "Selected"}], "root", 0, outcomes.append)
    assert outcomes == [False]
    assert options["placement_ref"]() is None
    assert pending == []


def test_meeting_selection_deleted_target_acknowledges_failure_once():
    controller, selector, _ = _meeting()
    controller._insert_nodes = lambda *args, **kwargs: (
        kwargs["on_completed"](None) or False
    )
    MeetingTreeController._import_jwpubs(controller, ["one.jwpub"])
    outcomes = []
    selector.calls[0][1]["insert"]([], "section:removed", 0, outcomes.append)
    assert outcomes == [False]


def test_meeting_multiple_publications_preserve_explicit_drop_order():
    controller, selector, pending = _meeting()
    MeetingTreeController._import_jwpubs(controller, ["one.jwpub", "two.jwpub"], "root", 4)
    options = selector.calls[0][1]
    outcomes = []
    options["insert"]([{"id": "one"}], "root", 4, outcomes.append)
    pending[0][1]["on_completed"](pending[0][0][2])
    options["insert"]([{"id": "two"}], "root", 4, outcomes.append)
    assert pending[1][0][1] == 5
    pending[1][1]["on_completed"](pending[1][0][2])
    assert outcomes == [True, True]


def test_meeting_linked_copy_failure_completes_pending_insertion():
    operations = _Operations()
    outcomes, removed = [], []
    controller = SimpleNamespace(
        _tree_session=SimpleNamespace(
            owner_id="meeting:test", generation=1,
            add_pending=lambda *_args, **_kwargs: None,
            remove_pending=removed.append,
        ),
        _sync_folder="linked-folder", _tree_key="test",
        _media_tree_runtime=SimpleNamespace(operations=operations),
    )
    assert MeetingTreeController._queue_nodes_for_sync(
        controller, "root", 0, [{"id": "item", "type": "media"}],
        signal_name="media", on_completed=outcomes.append,
    )
    assert outcomes == []
    spec = operations.specs[0]
    if spec.failed is not None:
        spec.failed("copy failed", False)
    assert outcomes == [None]
    assert removed == [spec.operation_id]


def test_meeting_failed_save_restores_insertion_target():
    children = [{"id": "existing", "type": "media"}]
    controller = SimpleNamespace(
        _parse_list_id=lambda _: ("root", ""),
        _children_for_target=lambda *_: children,
        _save=lambda: False,
        _start_media_requests=lambda _: None,
        _emit_section_counts=lambda: None,
    )
    assert not MeetingTreeController._commit_inserted_nodes(
        controller, "root", 0, [{"id": "new", "type": "media"}], signal_name="media",
    )
    assert [node["id"] for node in children] == ["existing"]


class _Playlist(PlaylistEditImportMixin):
    def __init__(self, tmp_path, *, linked=False):
        self.lang = SimpleNamespace(jw_lang_service=None)
        self._pl = {
            "id": "source", "items": [], "markers": [],
            "sections": [{"id": "talk", "name": "Talk"}],
        }
        self._jwpub_origin_generation = 1
        self._jwpub_selection_factory = _Selector()
        self._profile_paths = SimpleNamespace(images_dir=tmp_path / "images")
        self._is_watched = linked
        self._watched_path = str(tmp_path / "linked") if linked else ""
        if linked:
            Path(self._watched_path).mkdir()
        self._jwpub_copy_operations = set()
        self._wf_refresh_inflight = None
        self._wf_refresh_pending = False
        self.pending = {}
        self.saves = []
        self.cleanup_paths = []
        self._tree_session = SimpleNamespace(
            owner_id="playlist:source", generation=1,
            insert_media=lambda target, index, items: insert_playlist_media(self._pl, target, index, items),
            add_pending=lambda item, **_: self.pending.update({item["id"]: item}),
            remove_pending=lambda item_id: self.pending.pop(item_id, None),
            refresh=lambda: None,
        )
        self._media_tree_runtime = SimpleNamespace(
            operations=_Operations(),
            snapshots=SimpleNamespace(request=lambda _key, callback, **_: callback()),
        )
        self._watched_folder_file_store = WatchedFolderFileStore()

    def tr(self, source):
        return source

    def _save(self):
        self.saves.append(self._pl)

    def _reconcile_playlist(self):
        pass

    def _sync_playlist_chrome(self, **_kwargs):
        pass

    def _schedule_manifest_save(self, folder, playlist):
        self.saves.append((folder, playlist))


def _choose(playlist, items, *, target="section:talk", index=0):
    playlist._import_jwpubs(["one.jwpub", "two.jwpub"])
    options = playlist._jwpub_selection_factory.calls[0][1]
    outcomes = []
    options["insert"](items, target, index, outcomes.append)
    return options, outcomes


def _finish_rollback(playlist):
    spec = playlist._media_tree_runtime.operations.specs[-1]
    assert spec.operation_type == "linked_folder_rollback"
    value = spec.runner(lambda _: None, SimpleNamespace(is_set=lambda: False))
    spec.commit(value)


def test_playlist_selection_inserts_ordered_canonical_batch_once(tmp_path):
    playlist = _Playlist(tmp_path)
    items = [
        {"id": "one", "title": "Image", "url": "slide.jpg", "type": "image"},
        {"id": "two", "title": "Audio", "url": "", "type": "audio", "key_symbol": "pub", "meps_language": 5},
    ]
    options, outcomes = _choose(playlist, items)
    assert options["list_id"] is None
    assert outcomes == [True]
    assert [item["id"] for item in playlist._pl["items"]] == ["one", "two"]
    assert playlist._pl["items"][1]["meps_language"] == 5
    assert len(playlist.saves) == 1


def test_playlist_failed_save_restores_batch_and_acknowledges_failure(tmp_path):
    playlist = _Playlist(tmp_path)

    def fail_save():
        raise OSError("write failed")

    playlist._save = fail_save
    _, outcomes = _choose(playlist, [{"id": "one", "url": "one.jpg"}])
    assert outcomes == [False]
    assert playlist._pl["items"] == []


@pytest.mark.parametrize("expiry", ["playlist", "generation", "origin", "target"])
def test_playlist_selection_rejects_stale_origin_or_target(tmp_path, expiry):
    playlist = _Playlist(tmp_path)
    playlist._import_jwpubs(["one.jwpub"])
    options = playlist._jwpub_selection_factory.calls[0][1]
    original = playlist._pl
    if expiry == "playlist":
        playlist._pl = {"id": "other", "items": []}
    elif expiry == "generation":
        playlist._tree_session.generation += 1
    elif expiry == "origin":
        playlist._jwpub_origin_generation += 1
    else:
        playlist._pl["sections"].clear()
    outcomes = []
    options["insert"]([{"id": "one", "url": "slide.jpg"}], "section:talk", 0, outcomes.append)
    assert outcomes == [False]
    assert original["items"] == []
    assert playlist.saves == []


def test_playlist_linked_selection_waits_for_ordered_batch_copy(tmp_path):
    playlist = _Playlist(tmp_path, linked=True)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"image")
    items = [
        {"id": "one", "url": str(source), "type": "video", "title": "Original title"},
        {"id": "two", "url": "https://example.org/audio.mp3", "type": "audio"},
        {"id": "three", "url": str(source), "type": "video"},
    ]
    _, outcomes = _choose(playlist, items)
    assert outcomes == []
    assert playlist._pl["items"] == []
    assert len(playlist._media_tree_runtime.operations.specs) == 1
    spec = playlist._media_tree_runtime.operations.specs[0]
    value = spec.runner(lambda _: None, SimpleNamespace(is_set=lambda: False))
    assert outcomes == []
    assert len(value.copies) == 1
    spec.commit(value)
    assert outcomes == [True]
    assert [item["id"] for item in playlist._pl["items"]] == ["one", "two", "three"]
    assert playlist._pl["items"][0]["title"] == "Original title"
    assert Path(playlist._pl["items"][0]["url"]).parent == Path(playlist._watched_path)
    assert len(playlist.saves) == 1
    assert not playlist.pending


def test_playlist_linked_copy_failure_rolls_back_only_batch_files(tmp_path):
    playlist = _Playlist(tmp_path, linked=True)
    source = tmp_path / "slide.jpg"
    source.write_bytes(b"image")
    preexisting = Path(playlist._watched_path) / "existing.jpg"
    preexisting.write_bytes(b"existing")
    _, outcomes = _choose(playlist, [
        {"id": "one", "url": str(source)},
        {"id": "two", "url": str(tmp_path / "missing.jpg")},
    ])
    spec = playlist._media_tree_runtime.operations.specs[0]
    with pytest.raises(OSError):
        spec.runner(lambda _: None, SimpleNamespace(is_set=lambda: False))
    spec.failed("copy failed", False)
    spec.cancelled()
    assert outcomes == [False]
    assert list(Path(playlist._watched_path).iterdir()) == [preexisting]
    assert source.is_file()
    assert not playlist.pending
    assert not playlist.saves


def test_playlist_multiple_publications_preserve_explicit_drop_order(tmp_path):
    playlist = _Playlist(tmp_path)
    playlist._import_jwpubs(["one.jwpub", "two.jwpub"], list_id="root", insert_index=0)
    options = playlist._jwpub_selection_factory.calls[0][1]
    outcomes = []
    options["insert"]([{"id": "one", "url": "one.jpg"}], "root", 0, outcomes.append)
    options["insert"]([{"id": "two", "url": "two.jpg"}], "root", 0, outcomes.append)
    assert [item["id"] for item in playlist._pl["items"]] == ["one", "two"]
    assert outcomes == [True, True]


def test_playlist_origin_retired_after_copy_completes_and_rolls_back(tmp_path):
    playlist = _Playlist(tmp_path, linked=True)
    source = tmp_path / "slide.jpg"
    source.write_bytes(b"image")
    _, outcomes = _choose(playlist, [{"id": "one", "url": str(source)}])
    spec = playlist._media_tree_runtime.operations.specs[0]
    value = spec.runner(lambda _: None, SimpleNamespace(is_set=lambda: False))
    playlist._jwpub_origin_generation += 1
    spec.cancelled()
    spec.cancelled()
    assert outcomes == []
    _finish_rollback(playlist)
    assert outcomes == [False]
    assert not value.copies[0].destination.exists()
    assert not playlist._pl["items"]
    assert source.is_file()


def test_playlist_deleted_target_after_copy_discards_only_new_files(tmp_path):
    playlist = _Playlist(tmp_path, linked=True)
    source = tmp_path / "slide.jpg"
    source.write_bytes(b"image")
    _, outcomes = _choose(playlist, [{"id": "one", "url": str(source)}])
    spec = playlist._media_tree_runtime.operations.specs[0]
    value = spec.runner(lambda _: None, SimpleNamespace(is_set=lambda: False))
    playlist._pl["sections"].clear()
    spec.commit(value)
    assert outcomes == []
    _finish_rollback(playlist)
    assert outcomes == [False]
    assert playlist._pl["items"] == []
    assert not value.copies[0].destination.exists()
    assert source.is_file()
    assert not playlist.saves


def test_public_talk_playlist_shortcut_forces_origin_placement(tmp_path):
    playlist = _Playlist(tmp_path)
    playlist._import_public_talk_media()
    paths, options = playlist._jwpub_selection_factory.calls[0]
    assert paths == []
    assert options["public_talk"] is True
    assert options["list_id"] is None
    assert options["placement_ref"]()["id"] == "source"
