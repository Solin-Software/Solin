"""Local meeting-folder behavior with deterministically scheduled background work."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.ingest.sync.journal import ReplicaSnapshot
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.meetings.linked_folder_sync import MeetingLinkedFolderSync, MeetingSyncIdentity
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.meetings.tree_types import clone_nodes, iter_nodes
from solin.widgets.meetings.tree_controller import MeetingTreeController


class _QueuedOperations:
    def __init__(self):
        self.pending = []

    def submit(self, spec):
        self.pending.append(spec)
        return True

    def cancel(self, operation_id):
        self.pending = [spec for spec in self.pending if spec.operation_id != operation_id]

    def take(self, operation_type):
        spec = next(spec for spec in self.pending if spec.operation_type == operation_type)
        self.pending.remove(spec)
        return spec

    def finish(self, spec):
        value = spec.runner(lambda _progress: None, CancellationFlag())
        spec.commit(value)

    def drain(self):
        for _ in range(12):
            if not self.pending:
                return
            self.finish(self.pending.pop(0))
        pytest.fail("Meeting folder refresh keeps scheduling itself without a filesystem change")


class _PendingTreeSession:
    owner_id = "meeting:test"
    generation = 1

    def __init__(self):
        self.pending = {}

    def add_pending(self, operation_id, nodes, **_placement):
        self.pending[operation_id] = nodes

    def remove_pending(self, operation_id):
        self.pending.pop(operation_id, None)

    def remove_pending_node(self, _node_id):
        return False

    def pending_nodes(self):
        return tuple(node for nodes in self.pending.values() for node in nodes)


class _LocalMeeting:
    """Real controller policies, storage and files; queued work and no presentation I/O."""

    set_sync_root = MeetingTreeController.set_sync_root
    inject_linked_folder_media = MeetingTreeController.inject_linked_folder_media
    _request_meeting_folder_scan = MeetingTreeController._request_meeting_folder_scan
    _request_sync_refresh = MeetingTreeController._request_sync_refresh
    _refresh_sync_availability = MeetingTreeController._refresh_sync_availability
    _apply_sync_discovery = MeetingTreeController._apply_sync_discovery
    _apply_sync_record = MeetingTreeController._apply_sync_record
    _cancel_sync_refresh = MeetingTreeController._cancel_sync_refresh
    _disable_sync = MeetingTreeController._disable_sync
    _detach_sync_state = MeetingTreeController._detach_sync_state
    _durable_detached_dir = MeetingTreeController._durable_detached_dir
    _apply_meeting_folder_scan = MeetingTreeController._apply_meeting_folder_scan
    _reconcile_local_meeting_folder = MeetingTreeController._reconcile_local_meeting_folder
    _adopt_existing_meeting_folder_source = (
        MeetingTreeController._adopt_existing_meeting_folder_source
    )
    _meeting_folder_target_list_id = MeetingTreeController._meeting_folder_target_list_id
    _find_section_by_code = MeetingTreeController._find_section_by_code
    _import_meeting_folder_source = MeetingTreeController._import_meeting_folder_source
    _insert_meeting_folder_nodes = MeetingTreeController._insert_meeting_folder_nodes
    _record_meeting_folder_import = MeetingTreeController._record_meeting_folder_import
    _manual_media_node = MeetingTreeController._manual_media_node
    _insert_nodes = MeetingTreeController._insert_nodes
    _commit_inserted_nodes = MeetingTreeController._commit_inserted_nodes
    _queue_nodes_for_sync = MeetingTreeController._queue_nodes_for_sync
    _queue_detached_meeting_nodes = MeetingTreeController._queue_detached_meeting_nodes
    _children_for_target = MeetingTreeController._children_for_target
    _parse_list_id = MeetingTreeController._parse_list_id
    _media_identity_records = MeetingTreeController._media_identity_records
    _find_node = MeetingTreeController._find_node
    _url_for_node = MeetingTreeController._url_for_node
    _replace_node = MeetingTreeController._replace_node
    _remember_deleted_sources = MeetingTreeController._remember_deleted_sources
    _cleanup_meeting_folder_import_for_removed_node = (
        MeetingTreeController._cleanup_meeting_folder_import_for_removed_node
    )
    removeItem = MeetingTreeController.removeItem
    _queue_linked_media_removal = MeetingTreeController._queue_linked_media_removal
    _save_and_emit_replace = MeetingTreeController._save_and_emit_replace
    _save = MeetingTreeController._save
    _save_local_cache = MeetingTreeController._save_local_cache
    _snapshot_storage_key = MeetingTreeController._snapshot_storage_key
    _on_snapshot_write_completed = MeetingTreeController._on_snapshot_write_completed

    def __init__(self, root, pub_type="mwb"):
        self.operations = _QueuedOperations()
        self._media_tree_runtime = SimpleNamespace(
            operations=self.operations,
            snapshots=SimpleNamespace(request=self._persist_snapshot),
        )
        signal = SimpleNamespace(emit=lambda *_args: None)
        self.chromeChanged = self.stateChanged = self.syncStateChanged = signal
        self.storageSaved = signal
        self._detach_pending_persistence = set()
        self._local_edit_revision = 0
        self._tree_session = _PendingTreeSession()
        self._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
        self._sync_retry_timer = SimpleNamespace(start=lambda _ms: None, stop=lambda: None)
        self._sync_identity = MeetingSyncIdentity(
            f"{pub_type}:2026-05-25:T:issue",
            pub_type,
            date(2026, 5, 25),
        )
        self._tree_key = self._sync_identity.tree_key
        self._sync_root = str(root)
        self._sync_folder = ""
        self._sync_enabled = False
        self._sync_available = True
        self._sync_snapshot = ReplicaSnapshot()
        self._sync_revision = self._sync_refresh_generation = 0
        self._sync_refresh_operation_id = ""
        self._sync_service = MeetingLinkedFolderSync(lambda _: 0)
        self._pending_sync_saves = {}
        self._hidden_canonical_media = {}
        self._canonical_nodes = []
        self._canonical_hash = ""
        self._canonical_reset_generation = 0
        self._deleted_source_keys = set()
        self._local_snapshot_generation = 0
        self._local_snapshot_operation_id = ""
        self._linked_folder_files = {}
        self._meeting_folder_imports = {}
        self._meeting_folder_pending_sources = set()
        self._meeting_folder_scan_generation = 0
        self._meeting_folder_scan_operation_id = ""
        self._resolved_urls = {}
        self._watched_folder_file_store = WatchedFolderFileStore()
        self._store = MeetingTreeStore(root.parent / "meeting-trees.json")
        self._nodes = [
            {
                "id": "default",
                "type": "section",
                "section_code": "lac" if pub_type == "mwb" else "public_talk",
                "children": [],
            }
        ]
        self._profile_paths = SimpleNamespace(embedded_dir=root.parent / "detached")
        self._arm_sync_save_timer = lambda: None
        self._flush_image_framing_save = lambda: True
        self._start_media_requests = lambda *_args: None
        self._emit_section_counts = lambda: None
        self._cancel_media_info_requests_for_item = lambda _id: None
        self._current_overview = lambda: None
        self._warn_sync_failed = lambda message: pytest.fail(message)
        self._schedule_sync_manifest_save = lambda: None
        self._generated_asset_roots = lambda: (str(root.parent / "generated"),)

    def refresh(self):
        self.inject_linked_folder_media(self._sync_root)
        self.operations.drain()

    def media(self):
        return [node for node in iter_nodes(self._nodes) if node.get("type") == "media"]

    def _submit_modal_operation(self, **options):
        value = options["runner"](lambda _progress: None, CancellationFlag())
        options["commit"](value)
        return True

    def _persist_snapshot(self, key, write):
        write()
        self._on_snapshot_write_completed(key, 1)
        return 1

    def parent(self):
        return None

    def activate(self, folder):
        record = self._sync_service.save_tree(
            folder,
            self._sync_identity,
            nodes=self._nodes,
            deleted_source_keys=set(),
            linked_folder_files=self._linked_folder_files,
            meeting_folder_imports=self._meeting_folder_imports,
            enable=True,
        )
        self._apply_sync_record(record)


@pytest.fixture
def local_meeting(tmp_path):
    root = tmp_path / "linked"
    folder = root / "2026-05-25 MW"
    folder.mkdir(parents=True)
    return _LocalMeeting(root), folder


def test_inactive_refresh_quiesces_and_persists_new_media(local_meeting):
    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")

    meeting.refresh()

    assert [node["media_ref"]["file_path"] for node in meeting.media()] == [str(source)]
    assert meeting._nodes[0]["children"] == meeting.media()
    assert not meeting._sync_enabled
    assert not (folder / ".solin_sync").exists()
    assert meeting._store.snapshot(meeting._tree_key).nodes == meeting._nodes


def test_inactive_refresh_does_not_restore_ui_deletion(local_meeting):
    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting._apply_meeting_folder_scan(
        meeting._watched_folder_file_store.scan_meeting_sources(folder.parent),
        "2026-05-25",
        "mwb",
    )
    meeting._sync_folder = str(folder)
    meeting._request_sync_refresh()
    spec = meeting.operations.take("meeting_sync_refresh")
    value = spec.runner(lambda _progress: None, CancellationFlag())

    meeting.removeItem(meeting.media()[0]["id"])
    meeting.operations.finish(meeting.operations.take("meeting_linked_media_remove"))
    spec.commit(value)

    assert meeting.media() == []
    assert not source.exists()


def test_inactive_scan_removes_missing_direct_media(local_meeting):
    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting._apply_meeting_folder_scan(
        meeting._watched_folder_file_store.scan_meeting_sources(folder.parent),
        "2026-05-25",
        "mwb",
    )
    source.unlink()

    meeting._apply_meeting_folder_scan(
        meeting._watched_folder_file_store.scan_meeting_sources(folder.parent),
        "2026-05-25",
        "mwb",
    )

    assert meeting.media() == []
    assert meeting._meeting_folder_imports == {}


@pytest.mark.parametrize(
    "pub_type,tag,section", [("mwb", "MW", "lac"), ("wt", "WE", "public_talk")]
)
def test_disabled_import_uses_default_section_and_survives_restart(
    tmp_path, pub_type, tag, section
):
    root = tmp_path / "linked"
    folder = root / f"2026-05-25 {tag}"
    folder.mkdir(parents=True)
    source = folder / "talk.mp4"
    source.write_bytes(b"video")
    meeting = _LocalMeeting(root, pub_type)

    meeting.refresh()
    saved = meeting._store.snapshot(meeting._tree_key)
    restarted = _LocalMeeting(root, pub_type)
    restarted._nodes = clone_nodes(saved.nodes)
    restarted._meeting_folder_imports = saved.meeting_folder_imports
    restarted._linked_folder_files = saved.linked_folder_files
    MeetingTreeController._restore_local_sync_binding(restarted, saved)
    restarted.refresh()

    assert restarted._nodes[0]["section_code"] == section
    assert restarted._nodes[0]["children"] == restarted.media()
    assert len(restarted.media()) == 1
    assert restarted._store.snapshot(restarted._tree_key).nodes == restarted._nodes


def test_disable_then_ui_delete_and_reinclude_identical_media(local_meeting, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting.refresh()
    meeting.activate(folder)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    meeting._disable_sync()
    meeting.operations.drain()
    assert not meeting._sync_enabled
    assert source.exists()
    assert len(meeting.media()) == 1
    meeting.removeItem(meeting.media()[0]["id"])
    meeting.operations.drain()
    meeting.refresh()
    assert meeting.media() == []
    assert meeting._meeting_folder_imports == {}
    source.write_bytes(b"video")
    meeting.refresh()
    assert len(meeting.media()) == 1
    assert not (folder / ".solin_sync").exists()


def test_missing_source_then_identical_readdition_is_imported(local_meeting):
    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting.refresh()
    source.unlink()
    meeting.refresh()
    assert meeting.media() == []
    source.write_bytes(b"video")
    meeting.refresh()
    assert len(meeting.media()) == 1


def test_failed_physical_removal_keeps_local_suppression(local_meeting, monkeypatch):
    meeting, folder = local_meeting
    source = folder / "locked.mp4"
    source.write_bytes(b"video")
    meeting.refresh()
    meeting.removeItem(meeting.media()[0]["id"])
    spec = meeting.operations.take("meeting_linked_media_remove")

    def locked(*_args):
        raise PermissionError("locked by provider")

    monkeypatch.setattr(meeting._watched_folder_file_store, "remove_file_inside", locked)
    with pytest.raises(PermissionError):
        meeting.operations.finish(spec)
    meeting.refresh()

    assert source.exists()
    assert meeting.media() == []
    record = next(iter(meeting._meeting_folder_imports.values()))
    assert record["node_ids"] == []
    assert (
        meeting._store.snapshot(meeting._tree_key).meeting_folder_imports
        == meeting._meeting_folder_imports
    )


@pytest.mark.parametrize("missing", ["root", "folder"])
def test_folder_unavailability_preserves_local_tree(local_meeting, missing):
    meeting, folder = local_meeting
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting.refresh()
    before = clone_nodes(meeting._nodes)
    target = folder.parent if missing == "root" else folder
    moved = target.with_name(target.name + "-offline")
    target.rename(moved)

    meeting.refresh()

    assert meeting._nodes == before
    assert meeting._meeting_folder_imports
    assert not meeting._sync_enabled
    moved.rename(target)
    meeting.refresh()
    assert meeting._nodes == before


def test_missing_sources_preserve_canonical_media_and_converted_outputs(local_meeting):
    meeting, folder = local_meeting
    generated = folder / "official.mp4"
    durable = folder.parent.parent / "converted" / "page.jpg"
    durable.parent.mkdir()
    durable.write_bytes(b"image")
    meeting._nodes[0]["children"] = [
        {
            "id": "official",
            "type": "media",
            "meeting_generated": True,
            "linked_folder_source": str(folder),
            "media_ref": {"file_path": str(generated)},
        },
        {
            "id": "page",
            "type": "media",
            "linked_folder_source": str(folder),
            "media_ref": {"file_path": str(durable)},
        },
    ]
    meeting._meeting_folder_imports = {
        "slides": {
            "path": str(folder / "slides.pdf"),
            "kind": "pdf",
            "status": "processed",
            "signature": {},
            "node_ids": ["page"],
        }
    }

    meeting.refresh()

    assert {node["id"] for node in meeting.media()} == {"official", "page"}
    assert "slides" in meeting._meeting_folder_imports
    assert durable.exists()


def test_scan_captured_before_an_insertion_cannot_prune_the_new_item(local_meeting):
    meeting, folder = local_meeting
    meeting._request_meeting_folder_scan(str(folder.parent))
    spec = meeting.operations.take("meeting_folder_scan")
    value = spec.runner(lambda _progress: None, CancellationFlag())
    source = folder / "new.mp4"
    source.write_bytes(b"video")
    meeting._apply_meeting_folder_scan(
        meeting._watched_folder_file_store.scan_meeting_sources(folder.parent),
        "2026-05-25",
        "mwb",
    )

    spec.commit(value)
    meeting.operations.drain()

    assert len(meeting.media()) == 1
    assert source.exists()


@pytest.mark.parametrize("persist_edit", [True, False])
def test_received_disable_detaches_current_edits_before_removing_cache(local_meeting, persist_edit):
    from solin.core.ingest.sync.activation import ActivationStore

    meeting, folder = local_meeting
    cache = folder / ".solin_cache" / "page.jpg"
    cache.parent.mkdir()
    cache.write_bytes(b"image")
    meeting._nodes[0]["children"] = [
        {
            "id": "page",
            "type": "media",
            "title": "original",
            "media_ref": {"file_path": str(cache)},
            "linked_folder_source": str(folder),
        }
    ]
    meeting.activate(folder)
    ActivationStore(folder, "meeting").remove(meeting._sync_snapshot.document_id)
    meeting._request_sync_refresh()
    spec = meeting.operations.take("meeting_sync_refresh")
    value = spec.runner(lambda _progress: None, CancellationFlag())
    assert cache.exists()
    meeting.media()[0]["title"] = "edited during detach"
    if persist_edit:
        meeting._save_local_cache()

    spec.commit(value)
    meeting.operations.drain()

    assert not meeting._sync_enabled
    assert meeting.media()[0]["title"] == "edited during detach"
    detached = meeting.media()[0]["media_ref"]["file_path"]
    assert detached != str(cache)
    assert not cache.exists()
    assert Path(detached).read_bytes() == b"image"
    assert meeting._store.snapshot(meeting._tree_key).nodes == meeting._nodes


def test_received_disable_preserves_cache_until_snapshot_write_succeeds(local_meeting, monkeypatch):
    from solin.core.ingest.sync.activation import ActivationStore

    meeting, folder = local_meeting
    cache = folder / ".solin_cache" / "page.jpg"
    cache.parent.mkdir()
    cache.write_bytes(b"image")
    meeting._nodes[0]["children"] = [
        {
            "id": "page",
            "type": "media",
            "media_ref": {"file_path": str(cache)},
            "linked_folder_source": str(folder),
        }
    ]
    meeting.activate(folder)
    meeting._save_local_cache()
    pending = {}

    def enqueue(key, write):
        pending[key] = write
        return 1

    meeting._media_tree_runtime.snapshots.request = enqueue
    ActivationStore(folder, "meeting").remove(meeting._sync_snapshot.document_id)
    meeting._request_sync_refresh()
    meeting.operations.drain()
    assert cache.exists()
    assert not meeting._sync_enabled
    assert meeting._tree_key in meeting._detach_pending_persistence
    # Watcher events during a failed/delayed snapshot must not remove its bytes.
    meeting.refresh()
    assert cache.exists()
    assert meeting._store.snapshot(meeting._tree_key).linked_sync["enabled"]
    key = meeting._snapshot_storage_key()

    def denied(*_args, **_kwargs):
        raise PermissionError("local snapshot is locked")

    with monkeypatch.context() as blocked:
        blocked.setattr(meeting._store, "save", denied)
        with pytest.raises(PermissionError):
            pending[key]()
    assert cache.exists()
    pending[key]()
    meeting._on_snapshot_write_completed(key, 1)
    meeting.operations.drain()
    assert not cache.exists()
    assert meeting._detach_pending_persistence == set()
    assert meeting._store.snapshot(meeting._tree_key).nodes == meeting._nodes


def test_failed_received_detachment_preserves_original_tree_and_cache(local_meeting, monkeypatch):
    from solin.core.ingest.sync.activation import ActivationStore

    meeting, folder = local_meeting
    cache = folder / ".solin_cache" / "page.jpg"
    cache.parent.mkdir()
    cache.write_bytes(b"image")
    meeting._nodes[0]["children"] = [
        {
            "id": "page",
            "type": "media",
            "media_ref": {"file_path": str(cache)},
            "linked_folder_source": str(folder),
        }
    ]
    meeting.activate(folder)
    before = clone_nodes(meeting._nodes)
    ActivationStore(folder, "meeting").remove(meeting._sync_snapshot.document_id)

    def denied(*_args, **_kwargs):
        raise PermissionError("detached storage is locked")

    monkeypatch.setattr(meeting._sync_service, "detach_cache_references", denied)
    meeting._request_sync_refresh()
    meeting.operations.drain()

    assert meeting._nodes == before
    assert cache.exists()
    assert meeting._sync_transport_pending


def test_active_scan_preserves_unavailable_manual_media(local_meeting):
    meeting, folder = local_meeting
    meeting._nodes[0]["children"] = [
        {
            "id": "shared",
            "type": "media",
            "linked_folder_source": str(folder),
            "media_ref": {"file_path": str(folder / "unavailable.mp4")},
        }
    ]
    meeting._sync_enabled = True

    meeting._apply_meeting_folder_scan(
        meeting._watched_folder_file_store.scan_meeting_sources(folder.parent),
        "2026-05-25",
        "mwb",
    )

    assert [node["id"] for node in meeting.media()] == ["shared"]


@pytest.mark.parametrize("deactivation", ["received", "local"])
def test_pending_prepared_copy_survives_disable_without_missing_cache(
    local_meeting, monkeypatch, deactivation
):
    from solin.core.ingest.sync.activation import ActivationStore
    from PySide6.QtWidgets import QMessageBox

    meeting, folder = local_meeting
    source = folder.parent.parent / "generated" / "photo.jpg"
    source.parent.mkdir()
    source.write_bytes(b"image")
    meeting.activate(folder)
    meeting._insert_nodes("section:default", 0, [meeting._manual_media_node(str(source))])
    spec = meeting.operations.take("meeting_media_copy")
    prepared = spec.runner(lambda _progress: None, CancellationFlag())
    copied = Path(prepared.nodes[0]["media_ref"]["file_path"])
    assert copied.is_relative_to(folder / ".solin_cache")
    if deactivation == "received":
        ActivationStore(folder, "meeting").remove(meeting._sync_snapshot.document_id)
        meeting._request_sync_refresh()
    else:
        monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
        meeting._disable_sync()
    meeting.operations.drain()
    assert copied.exists()
    assert not meeting._sync_enabled

    spec.commit(prepared)
    meeting.operations.drain()

    assert len(meeting.media()) == 1
    path = Path(meeting.media()[0]["media_ref"]["file_path"])
    assert path.read_bytes() == b"image"
    assert not path.is_relative_to(folder)
    assert not copied.exists()
    assert not meeting._tree_session.pending_nodes()
    assert meeting._store.snapshot(meeting._tree_key).nodes == meeting._nodes


def test_pending_deletion_after_disable_does_not_recreate_sync_or_block_readdition(local_meeting):
    from solin.core.ingest.sync.activation import ActivationStore

    meeting, folder = local_meeting
    source = folder / "photo.jpg"
    source.write_bytes(b"image")
    meeting.refresh()
    meeting.activate(folder)
    meeting.removeItem(meeting.media()[0]["id"])
    spec = meeting.operations.take("meeting_linked_media_remove")
    ActivationStore(folder, "meeting").remove(meeting._sync_snapshot.document_id)
    meeting._request_sync_refresh()
    meeting.operations.drain()
    assert source.exists()
    assert not meeting._sync_enabled

    meeting.operations.finish(spec)
    assert not source.exists()
    assert not (folder / ".solin_sync").exists()
    source.write_bytes(b"image")
    meeting.refresh()

    assert len(meeting.media()) == 1


def test_old_deletion_cannot_remove_a_reactivated_document_resource(local_meeting):
    from solin.core.meetings.linked_folder_sync import MeetingSyncInactive

    meeting, folder = local_meeting
    source = folder / "photo.jpg"
    source.write_bytes(b"image")
    meeting.refresh()
    original = clone_nodes(meeting._nodes)
    meeting.activate(folder)
    meeting.removeItem(meeting.media()[0]["id"])
    spec = meeting.operations.take("meeting_linked_media_remove")
    meeting._sync_service.deactivate_tree(folder, meeting._sync_identity)
    meeting._nodes = original
    meeting.activate(folder)

    with pytest.raises(MeetingSyncInactive, match="another.*generation"):
        meeting.operations.finish(spec)

    assert source.exists()
    assert len(meeting.media()) == 1


def test_failed_source_scan_preserves_tree_and_processing_records(local_meeting, monkeypatch):
    meeting, folder = local_meeting
    source = folder / "photo.jpg"
    source.write_bytes(b"image")
    meeting.refresh()
    before = clone_nodes(meeting._nodes)
    records = meeting._store.snapshot(meeting._tree_key).meeting_folder_imports

    def unavailable(_root):
        raise PermissionError("provider cannot enumerate the folder")

    monkeypatch.setattr(meeting._watched_folder_file_store, "scan_meeting_sources", unavailable)
    meeting._request_meeting_folder_scan(str(folder.parent))
    spec = meeting.operations.take("meeting_folder_scan")
    with pytest.raises(PermissionError):
        meeting.operations.finish(spec)
    spec.failed("unavailable", False)

    assert meeting._nodes == before
    assert meeting._meeting_folder_imports == records


def test_cancelled_local_insertion_releases_cleanup_barrier(local_meeting):
    meeting, folder = local_meeting
    cache = folder / ".solin_cache" / "photo.jpg"
    cache.parent.mkdir()
    cache.write_bytes(b"image")
    callbacks = []
    meeting._sync_folder = str(folder)
    meeting._queue_detached_meeting_nodes(
        "section:default",
        0,
        [meeting._manual_media_node(str(cache))],
        folder,
        signal_name="media",
        on_completed=callbacks.append,
    )
    spec = meeting.operations.take("meeting_local_media_copy")

    spec.cancelled()
    meeting.operations.drain()

    assert not meeting._tree_session.pending_nodes()
    assert callbacks == [None]
    assert not cache.exists()
    assert meeting.media() == []


def test_explicit_disable_keeps_edits_received_during_copy(local_meeting, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    meeting, folder = local_meeting
    source = folder / "photo.jpg"
    source.write_bytes(b"image")
    meeting.refresh()
    meeting.activate(folder)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    def complete_with_edit(**options):
        value = options["runner"](lambda _progress: None, CancellationFlag())
        meeting.media()[0]["title"] = "updated while disabling"
        options["commit"](value)
        return True

    meeting._submit_modal_operation = complete_with_edit
    meeting._disable_sync()
    meeting.operations.drain()

    assert not meeting._sync_enabled
    assert meeting.media()[0]["title"] == "updated while disabling"
    assert meeting._store.snapshot(meeting._tree_key).nodes == meeting._nodes


def test_prepared_copy_requeues_originals_after_document_reactivation(local_meeting):
    meeting, folder = local_meeting
    source = folder.parent.parent / "generated" / "photo.jpg"
    source.parent.mkdir()
    source.write_bytes(b"image")
    meeting.activate(folder)
    first_document = meeting._sync_snapshot.document_id
    meeting._insert_nodes("section:default", 0, [meeting._manual_media_node(str(source))])
    spec = meeting.operations.take("meeting_media_copy")
    prepared = spec.runner(lambda _progress: None, CancellationFlag())
    meeting._sync_service.deactivate_tree(folder, meeting._sync_identity)
    meeting.activate(folder)

    spec.commit(prepared)
    meeting.operations.drain()

    assert meeting._sync_snapshot.document_id != first_document
    assert len(meeting.media()) == 1
    assert Path(meeting.media()[0]["media_ref"]["file_path"]).read_bytes() == b"image"
    assert not meeting._tree_session.pending_nodes()
