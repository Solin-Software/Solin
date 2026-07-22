from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.ui.qml.media_tree.meeting_session import MeetingTreeSession
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


class _InlineWorkers:
    def submit(self, _name: str, target):
        target()
        return object()


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
        presentation_workers=_InlineWorkers(),
    )


def _presented_node(session: MeetingTreeSession, node_id: str) -> dict[str, object]:
    stack = list(session.source.treeData)
    while stack:
        node = stack.pop()
        if node["id"] == node_id:
            return node
        stack.extend(node["children"])
    raise AssertionError(f"Missing presented node {node_id!r}")


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
    session._probe_requests.drain_now()
    session._publish_now()

    assert _presented_node(session, "media-1")["url"] == "resolved.mp4"
    assert runtime.probes.requests[0]["source"] == "resolved.mp4"

    nodes[0]["title"] = "Updated"
    session.refresh(nodes, resolved_urls={"media-1": "resolved.mp4"})
    session._publish_now()
    assert _presented_node(session, "media-1")["title"] == "Updated"
    session.close()


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
    session._publish_now()
    nodes[0]["media_ref"]["file_path"] = "second.mp4"
    session.refresh(nodes)
    session._publish_now()
    previous_revision = session.source.revision

    session.activate("mwb:week", nodes)
    session._publish_now()

    assert session.source.revision > previous_revision
    assert _presented_node(session, "media-1")["sourceRevision"] == 1
    session.close()


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
    session.close()


def test_failed_initial_snapshot_does_not_leave_the_meeting_tree_locked(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    session = MeetingTreeSession(
        runtime,
        lambda _node, item_id, _source: tmp_path / f"{item_id}.jpg",
    )
    nodes = [{
        "id": "invalid",
        "type": "media",
        "media_type": "document",
        "media_ref": {},
    }]

    session.activate("mwb:week", nodes)
    session._publish_now()

    assert session.source.transitioning is False
    assert session.source.treeId == "meeting:mwb:week"
    assert session.source.treeData == []
    assert session.source.error

    nodes[0]["media_type"] = "video"
    session.source.retry()
    session._publish_now()

    assert session.source.transitioning is False
    assert session.source.error == ""
    assert _presented_node(session, "invalid")["mediaType"] == "video"
    session.close()
