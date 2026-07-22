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
from solin.ui.qml.media_tree.roles import MediaTreeRole
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


def _runtime():
    return SimpleNamespace(
        registry=_Registry(),
        operations=_Operations(),
        probes=_Probes(),
        thumbnails=_Thumbnails(),
    )


def _playlist() -> dict:
    return {
        "id": "playlist-1",
        "name": "Test",
        "items": [{"id": "a", "title": "A", "url": "a.mp4", "type": "video"}],
        "sections": [],
        "markers": [],
    }


def test_session_publishes_full_snapshots_and_probes_sources(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()

    session.activate(playlist)

    assert session.model.treeId == "playlist:playlist-1"
    assert session.model.mediaCount == 1
    assert runtime.probes.requests[0]["source"] == "a.mp4"

    playlist["items"][0]["title"] = "Renamed"
    session.refresh(probe_changed_sources=False)
    session._publish_now()

    index = session.model.index_for_id("a")
    assert session.model.data(index, int(MediaTreeRole.TITLE)) == "Renamed"


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

    index = session.model.index_for_id("pending")
    assert index.isValid()
    assert session.model.data(index, int(MediaTreeRole.OPERATION_STATE)) == "copying"
    assert session.model.data(index, int(MediaTreeRole.OPERATION_PROGRESS)) == 0.5
    assert session.model.data(index, int(MediaTreeRole.CAN_DRAG)) is False

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
    session._publish_now()

    index = session.model.index_for_id("a")
    assert session.model.data(index, int(MediaTreeRole.SOURCE_REVISION)) == 1
    assert runtime.probes.requests[-1]["source"] == "replacement.mp4"


def test_request_nodes_reprobes_only_known_persisted_nodes(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    playlist = _playlist()
    playlist["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )
    session.activate(playlist)
    runtime.probes.requests.clear()

    session.request_nodes(["b", "unknown", "b"])

    assert [request["node_id"] for request in runtime.probes.requests] == ["b"]
    assert runtime.probes.requests[0]["source"] == "b.mp4"


def test_update_playlist_rebinds_same_tree_without_reprobing_unchanged_nodes(
    tmp_path: Path,
):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    session.activate(_playlist())
    runtime.probes.requests.clear()
    updated = _playlist()
    updated["items"][0]["title"] = "Updated"

    session.update_playlist(updated)
    session._publish_now()

    assert session.playlist is updated
    assert runtime.probes.requests == []
    index = session.model.index_for_id("a")
    assert session.model.data(index, int(MediaTreeRole.TITLE)) == "Updated"


def test_reactivating_same_playlist_keeps_revisions_monotonic(tmp_path: Path):
    runtime = _runtime()
    session = PlaylistTreeSession(runtime, lambda item_id: tmp_path / f"{item_id}.jpg")
    loading = _playlist()
    session.activate(loading)
    first_revision = session.model.revision
    loaded = _playlist()
    loaded["items"].append(
        {"id": "b", "title": "B", "url": "b.mp4", "type": "video"}
    )

    session.activate(loaded)

    assert session.model.revision > first_revision
    assert session.model.contains("b")


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
