from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.ui.qml.media_tree.meeting_session import MeetingTreeSession
from solin.ui.qml.media_tree.roles import MediaTreeRole
from solin.ui.qml.media_tree.state import MediaPresentationState
from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationRecord,
    MediaOperationState,
)


_APP = QCoreApplication.instance() or QCoreApplication([])


class _Registry(QObject):
    stateChanged = Signal(str, str)

    def state(self, _owner_id: str, _node_id: str) -> MediaPresentationState:
        return MediaPresentationState()


class _Operations(QObject):
    operationChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self.discarded: list[str] = []

    def cancel(self, operation_id: str) -> None:
        self.cancelled.append(operation_id)

    def discard(self, operation_id: str) -> bool:
        self.discarded.append(operation_id)
        return True

    def cancel_scope(self, _scope_id: str) -> None:
        pass


class _Thumbnails(QObject):
    thumbnailStored = Signal(str, str)


class _Probes:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs) -> None:
        self.requests.append(kwargs)

    def remove(self, _owner_id: str, _node_id: str) -> None:
        pass

    def clear_owner(self, _owner_id: str) -> None:
        pass


def _runtime():
    return SimpleNamespace(
        registry=_Registry(),
        operations=_Operations(),
        probes=_Probes(),
        thumbnails=_Thumbnails(),
    )


def test_meeting_session_uses_resolved_url_and_reconciles(tmp_path: Path) -> None:
    runtime = _runtime()
    session = MeetingTreeSession(
        runtime,
        lambda _node, item_id, _source: tmp_path / f"{item_id}.jpg",
        badge_provider=lambda _kind: "Video",
    )
    nodes = [{
        "id": "media-1",
        "type": "media",
        "title": "Talk",
        "media_ref": {"file_path": "fallback.mp4"},
    }]

    session.activate("mwb:week", nodes, resolved_urls={"media-1": "resolved.mp4"})

    index = session.model.index_for_id("media-1")
    assert session.model.data(index, int(MediaTreeRole.URL)) == "resolved.mp4"
    assert runtime.probes.requests[0]["source"] == "resolved.mp4"

    nodes[0]["title"] = "Updated"
    session.refresh(nodes, resolved_urls={"media-1": "resolved.mp4"})
    session._publish_now()
    assert session.model.data(index, int(MediaTreeRole.TITLE)) == "Updated"


def test_reactivating_same_meeting_keeps_snapshot_and_source_revisions(tmp_path: Path):
    runtime = _runtime()
    session = MeetingTreeSession(
        runtime,
        lambda _node, item_id, _source: tmp_path / f"{item_id}.jpg",
    )
    nodes = [{
        "id": "media-1",
        "type": "media",
        "media_ref": {"file_path": "first.mp4"},
    }]
    session.activate("mwb:week", nodes)
    nodes[0]["media_ref"]["file_path"] = "second.mp4"
    session.refresh(nodes)
    session._publish_now()
    previous_revision = session.model.revision

    session.activate("mwb:week", nodes)

    index = session.model.index_for_id("media-1")
    assert session.model.revision > previous_revision
    assert session.model.data(index, int(MediaTreeRole.SOURCE_REVISION)) == 1


def test_failed_pending_node_can_be_removed(tmp_path: Path) -> None:
    runtime = _runtime()
    session = MeetingTreeSession(
        runtime,
        lambda _node, item_id, _source: tmp_path / f"{item_id}.jpg",
    )
    session.activate("mwb:week", [])
    pending = [{"id": "pending", "type": "media", "media_ref": {}}]
    session.add_pending("copy-1", pending, target_list_id="root", insert_index=0)
    runtime.operations.operationChanged.emit(
        MediaOperationRecord(
            operation_id="copy-1",
            scope_id=session.owner_id,
            operation_type="meeting_media_copy",
            presentation=MediaOperationPresentation.TREE_LOCAL,
            state=MediaOperationState.FAILED,
            retryable=True,
        )
    )

    assert session.remove_pending_node("pending")
    assert runtime.operations.cancelled == ["copy-1"]
    assert runtime.operations.discarded == ["copy-1"]
