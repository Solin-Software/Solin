from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationRecord,
    MediaOperationState,
)
from solin.ui.qml.media_tree.playlist_session import PlaylistTreeSession
from solin.ui.qml.media_tree.state import MediaPresentationState
from solin.ui.qml.playlist.bridge import PlaylistEditBridge


_APP = QCoreApplication.instance() or QCoreApplication([])


class _Registry(QObject):
    stateChanged = Signal(str, str)

    def state(self, _owner_id: str, _node_id: str) -> MediaPresentationState:
        return MediaPresentationState()


class _Operations(QObject):
    operationChanged = Signal(object)

    def cancel_scope(self, _scope_id: str) -> None:
        pass

    def cancel(self, _operation_id: str) -> None:
        pass

    def discard(self, _operation_id: str) -> bool:
        return True


class _Thumbnails(QObject):
    thumbnailStored = Signal(str, str)


class _InlineWorkers:
    def submit(self, _name: str, target):
        target()
        return object()


class _ControlledWorkers:
    def __init__(self) -> None:
        self.tasks = []

    def submit(self, _name: str, target):
        self.tasks.append(target)
        return object()


class _Probes:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.removed: list[tuple[str, str]] = []
        self.cleared: list[str] = []

    def request(self, **kwargs) -> None:
        self.requests.append(kwargs)

    def remove(self, owner_id: str, node_id: str) -> None:
        self.removed.append((owner_id, node_id))

    def clear_owner(self, owner_id: str) -> None:
        self.cleared.append(owner_id)


def _runtime(workers=None):
    return SimpleNamespace(
        registry=_Registry(),
        operations=_Operations(),
        probes=_Probes(),
        thumbnails=_Thumbnails(),
        presentation_workers=workers or _InlineWorkers(),
    )


def _playlist() -> dict:
    return {
        "id": "playlist-1",
        "name": "Test",
        "items": [{"id": "a", "title": "A", "url": "a.mp4", "type": "video"}],
        "sections": [],
        "markers": [],
    }


def _presented_node(session: PlaylistTreeSession, node_id: str) -> dict[str, object]:
    stack = list(session.source.treeData)
    while stack:
        node = stack.pop()
        if node["id"] == node_id:
            return node
        stack.extend(node["children"])
    raise AssertionError(f"Missing presented node {node_id!r}")


def test_session_publishes_full_snapshots_and_probes_sources(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()

    session.activate(playlist)
    session._probe_requests.drain_now()
    session._publish_now()

    assert session.source.treeId == "playlist:playlist-1"
    assert session.source.mediaCount == 1
    assert runtime.probes.requests[0]["source"] == "a.mp4"

    playlist["items"][0]["title"] = "Renamed"
    session.refresh(probe_changed_sources=False)
    session._publish_now()

    assert _presented_node(session, "a")["title"] == "Renamed"


def test_pending_copy_is_a_real_media_row_with_operation_state(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    session.activate(_playlist())
    pending = {"id": "pending", "title": "Large", "url": "source.mp4", "type": "video"}
    session.add_pending(pending, target_list_id="root", insert_index=0)
    runtime.operations.operationChanged.emit(
        MediaOperationRecord(
            operation_id="copy-1",
            scope_id=session.owner_id,
            subject_id="pending",
            operation_type="linked_folder_copy",
            presentation=MediaOperationPresentation.TREE_LOCAL,
            state=MediaOperationState.COPYING,
            completed=5,
            total=10,
            cancellable=True,
        )
    )
    session._publish_now()

    presented = _presented_node(session, "pending")
    assert presented["operationState"] == "copying"
    assert presented["operationProgress"] == 0.5
    assert presented["canDrag"] is False

    runtime.operations.operationChanged.emit(
        MediaOperationRecord(
            operation_id="copy-1",
            scope_id=session.owner_id,
            subject_id="pending",
            operation_type="linked_folder_copy",
            presentation=MediaOperationPresentation.TREE_LOCAL,
            state=MediaOperationState.READY,
        )
    )

    assert session.operation_for("pending") is None


def test_source_change_increments_revision_and_reprobes(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    session.activate(playlist)
    playlist["items"][0]["url"] = "replacement.mp4"

    session.refresh()
    session._probe_requests.drain_now()
    session._publish_now()

    assert _presented_node(session, "a")["sourceRevision"] == 1
    assert runtime.probes.requests[-1]["source"] == "replacement.mp4"


def test_request_nodes_reprobes_only_known_persisted_nodes(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)
    session._probe_requests.drain_now()
    runtime.probes.requests.clear()

    session.request_nodes(["b", "unknown", "b"])
    session._probe_requests.drain_now()

    assert [request["node_id"] for request in runtime.probes.requests] == ["b"]
    assert runtime.probes.requests[0]["source"] == "b.mp4"


def test_update_playlist_rebinds_same_tree_without_reprobing_unchanged_nodes(
    tmp_path: Path,
):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    session.activate(_playlist())
    session._probe_requests.drain_now()
    runtime.probes.requests.clear()
    updated = _playlist()
    updated["items"][0]["title"] = "Updated"

    session.update_playlist(updated)
    session._publish_now()

    assert session.playlist is updated
    assert runtime.probes.requests == []
    assert _presented_node(session, "a")["title"] == "Updated"


def test_reactivating_same_playlist_keeps_revisions_monotonic(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    loading = _playlist()
    session.activate(loading)
    session._publish_now()
    first_revision = session.source.revision
    loaded = _playlist()
    loaded["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )

    session.activate(loaded)
    session._publish_now()

    assert session.source.revision > first_revision
    assert _presented_node(session, "b")["title"] == "B"


def test_stale_drag_command_is_rejected_after_domain_refresh(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    session.activate(playlist)
    expected = session.structure_revision
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.refresh(probe_changed_sources=False)

    assert not session.move("a", "root", 2, session.tree_id, expected)


def test_drag_command_from_another_tree_is_rejected(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)

    assert not session.move(
        "a",
        "root",
        1,
        "playlist:another-playlist",
        session.structure_revision,
    )
    assert [item["id"] for item in playlist["items"]] == ["a", "b"]


def test_session_discards_a_completed_build_from_the_previous_playlist(
    tmp_path: Path,
) -> None:
    workers = _ControlledWorkers()
    runtime = _runtime(workers)
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    first = _playlist()
    session.activate(first)
    session._publish_now()
    second = {
        **_playlist(),
        "id": "playlist-2",
        "items": [{"id": "b", "title": "B", "url": "b.mp4", "type": "video"}],
    }
    session.activate(second)
    session._publish_now()

    workers.tasks.pop(0)()
    session._builds._drain_results()
    assert session.source.transitioning is True
    assert session.source.treeId == "playlist:inactive"
    assert session.source.treeData == []

    session._publish_now()
    workers.tasks.pop(0)()
    session._builds._drain_results()
    assert session.source.transitioning is False
    assert _presented_node(session, "b")["title"] == "B"


def test_background_build_uses_an_isolated_playlist_capture(tmp_path: Path) -> None:
    workers = _ControlledWorkers()
    runtime = _runtime(workers)
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"][0]["type"] = "image"
    playlist["items"][0]["image_framing"] = {
        "version": 1,
        "zoom": 1.25,
        "norm_x": 0.1,
        "norm_y": 0.0,
    }
    session.activate(playlist)
    session._publish_now()

    playlist["items"][0]["title"] = "Mutated after dispatch"
    playlist["items"][0]["image_framing"]["zoom"] = 4.0
    workers.tasks.pop(0)()
    session._builds._drain_results()

    presented = _presented_node(session, "a")
    assert presented["title"] == "A"
    assert presented["imageFraming"] == {
        "version": 1,
        "zoom": 1.25,
        "norm_x": 0.1,
        "norm_y": 0.0,
    }


def test_probe_bursts_do_not_recapture_while_a_snapshot_build_is_active(
    tmp_path: Path,
) -> None:
    workers = _ControlledWorkers()
    runtime = _runtime(workers)
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    session.activate(_playlist())
    session._publish_now()
    first_revision = session._revision

    for _index in range(20):
        session._schedule_publish()
        session._publish_now()

    assert session._revision == first_revision
    assert len(workers.tasks) == 1

    workers.tasks.pop(0)()
    session._builds._drain_results()
    session._publish_now()

    assert session._revision == first_revision + 1
    assert len(workers.tasks) == 1


def test_failed_initial_snapshot_does_not_leave_the_tree_locked(tmp_path: Path) -> None:
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"][0]["type"] = "document"

    session.activate(playlist)
    session._publish_now()

    assert session.source.transitioning is False
    assert session.source.treeId == "playlist:playlist-1"
    assert session.source.treeData == []
    assert session.source.error

    playlist["items"][0]["type"] = "video"
    session.source.retry()
    session._publish_now()

    assert session.source.transitioning is False
    assert session.source.error == ""
    assert _presented_node(session, "a")["title"] == "A"


def test_consecutive_reorders_use_the_ui_post_removal_slot(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)

    assert session.move("a", "root", 1, session.tree_id, session.structure_revision)
    assert [item["id"] for item in playlist["items"]] == ["b", "a"]

    assert session.move("a", "root", 0, session.tree_id, session.structure_revision)
    assert [item["id"] for item in playlist["items"]] == ["a", "b"]


def test_reorder_discards_a_pre_move_snapshot_completed_during_drag(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)
    session._publish_now()
    initial_revision = session.source.revision
    expected_structure_revision = session.structure_revision

    session.source.beginInteraction()
    session._publish_now()
    assert session.source.revision == initial_revision

    assert session.move(
        "a",
        "root",
        1,
        session.tree_id,
        expected_structure_revision,
    )
    session.source.endInteraction()

    assert session.source.revision == initial_revision
    session._publish_now()
    assert [node["id"] for node in session.source.treeData] == ["b", "a"]


def test_bridge_requests_persistence_after_an_accepted_reorder(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)
    bridge = PlaylistEditBridge()
    bridge.attach_session(session)
    persistence_requests: list[bool] = []
    bridge.dragFinished.connect(lambda: persistence_requests.append(True))

    assert bridge.moveNode("a", "root", 1, session.tree_id, session.structure_revision)

    assert [item["id"] for item in playlist["items"]] == ["b", "a"]
    assert persistence_requests == [True]


def test_bridge_forwards_add_to_destination_intent():
    bridge = PlaylistEditBridge()
    requested = []
    bridge.addToDestinationSignal.connect(requested.append)

    bridge.addToDestination("media-1")

    assert requested == ["media-1"]


def test_bridge_forwards_set_as_idle_intent():
    bridge = PlaylistEditBridge()
    requested = []
    bridge.setAsIdleSignal.connect(requested.append)

    bridge.setAsIdle("media-1")

    assert requested == ["media-1"]
