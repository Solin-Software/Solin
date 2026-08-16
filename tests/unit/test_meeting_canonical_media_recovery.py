from __future__ import annotations

from types import SimpleNamespace

from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState
from solin.widgets.meetings.tree_controller import MeetingTreeController


class _Signal:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.calls.append(args)


def _controller(node: dict[str, object], *, sync_enabled: bool) -> SimpleNamespace:
    signal = _Signal()
    controller = SimpleNamespace(
        _tree_session=SimpleNamespace(owner_id="meeting:mwb"),
        _find_node=lambda item_id: node if item_id == node["id"] else None,
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner_id, _item_id: MediaPresentationState(
                    availability=MediaAvailability.MISSING,
                )
            )
        ),
        _sync_enabled=sync_enabled,
        _tree_key="mwb:2026-05-25:T:20260500",
        _canonical_counterpart=lambda candidate: candidate
        if candidate.get("meeting_generated")
        else None,
        _canonical_media_recovery_requests=set(),
        canonicalMediaRecoveryRequested=signal,
        _request_jw_resolution=lambda *_args, **_kwargs: None,
        _url_for_node=lambda candidate: str(
            (candidate.get("media_ref") or {}).get("file_path") or ""
        ),
    )
    controller._canonical_media_recovery_key = lambda candidate: (
        MeetingTreeController._canonical_media_recovery_key(controller, candidate)
    )
    controller._request_canonical_media_recovery = lambda candidate: (
        MeetingTreeController._request_canonical_media_recovery(controller, candidate)
    )
    return controller


def test_missing_visible_canonical_media_requests_one_local_recovery() -> None:
    path = "C:/cache/jwpub/mwb_T/x_20260500/images/image.jpg"
    node = {
        "id": "official-image",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:mwb:image:1",
        "media_ref": {"file_path": path},
    }
    controller = _controller(node, sync_enabled=False)

    MeetingTreeController._on_presentation_state_changed(
        controller,
        "meeting:mwb",
        "official-image",
    )
    MeetingTreeController._on_presentation_state_changed(
        controller,
        "meeting:mwb",
        "official-image",
    )

    assert controller.canonicalMediaRecoveryRequested.calls == [
        ("mwb:2026-05-25:T:20260500", path)
    ]


def test_missing_media_from_the_same_extract_requests_one_package_recovery() -> None:
    first_path = "C:/cache/jwpub/wcg_T/x_0/images/first.jpg"
    second_path = "C:/cache/jwpub/wcg_T/x_0/images/second.jpg"
    first = {
        "id": "first-image",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:wcg:image:1",
        "media_ref": {"file_path": first_path},
    }
    second = {
        "id": "second-image",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:wcg:image:2",
        "media_ref": {"file_path": second_path},
    }
    controller = _controller(first, sync_enabled=False)

    MeetingTreeController._on_presentation_state_changed(
        controller,
        "meeting:mwb",
        "first-image",
    )
    controller._find_node = lambda item_id: second if item_id == second["id"] else None
    MeetingTreeController._on_presentation_state_changed(
        controller,
        "meeting:mwb",
        "second-image",
    )

    assert controller.canonicalMediaRecoveryRequested.calls == [
        ("mwb:2026-05-25:T:20260500", first_path)
    ]


def test_missing_media_in_synced_tree_does_not_request_local_recovery() -> None:
    node = {
        "id": "official-image",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:mwb:image:1",
        "media_ref": {"file_path": "C:/cloud/image.jpg"},
    }
    controller = _controller(node, sync_enabled=True)

    MeetingTreeController._on_presentation_state_changed(
        controller,
        "meeting:mwb",
        "official-image",
    )

    assert controller.canonicalMediaRecoveryRequested.calls == []
