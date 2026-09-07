from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationRecord,
    MediaOperationState,
)
from solin.ui.qml.media_tree.playlist_presenter import PlaylistTreePresenter
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState


def _playlist() -> dict:
    return {
        "id": "playlist-1",
        "name": "Test",
        "sections": [
            {
                "id": "section-1",
                "name": "Main",
                "color_hue": 215,
                "collapsed": False,
                "position": 0,
            }
        ],
        "items": [
            {
                "id": "local-media",
                "title": "Local",
                "type": "video",
                "url": "C:/media/local.mp4",
                "section_id": "section-1",
            },
            {
                "id": "virtual-media",
                "title": "Virtual",
                "type": "video",
                "url": "https://example.test/virtual.mp4",
                "section_id": "section-1",
            },
        ],
        "markers": [],
    }


def _roles(node) -> dict[str, object]:
    return node.thawed_roles()


def test_presenter_counts_local_and_virtual_media_without_filesystem_queries() -> None:
    snapshot = PlaylistTreePresenter().build(_playlist(), revision=4)

    section = snapshot.roots[0]
    assert snapshot.tree_id == "playlist:playlist-1"
    assert snapshot.revision == 4
    assert _roles(section)["itemCount"] == 2
    assert [child.node_id for child in section.children] == [
        "local-media",
        "virtual-media",
    ]


def test_presenter_does_not_mark_temporary_cloud_failure_as_missing() -> None:
    runtime = {
        "virtual-media": MediaPresentationState(
            availability=MediaAvailability.TEMPORARILY_UNAVAILABLE,
            thumbnail_source="image://playlistthumbs/virtual-media/2",
        )
    }

    snapshot = PlaylistTreePresenter().build(
        _playlist(),
        revision=1,
        runtime_states=runtime,
    )
    virtual = snapshot.roots[0].children[1]
    roles = _roles(virtual)

    assert roles["isMissing"] is False
    assert roles["thumbSource"] == "image://playlistthumbs/virtual-media/2"
    assert roles["cloudVisible"] is True


def test_presenter_versions_provider_url_when_thumbnail_content_changes() -> None:
    runtime = {
        "virtual-media": MediaPresentationState(
            availability=MediaAvailability.AVAILABLE,
            thumbnail_source="image://playlistthumbs/virtual-media",
        )
    }

    snapshot = PlaylistTreePresenter().build(
        _playlist(),
        revision=1,
        runtime_states=runtime,
        source_revisions={"virtual-media": 4},
    )

    roles = _roles(snapshot.roots[0].children[1])
    assert roles["thumbSource"] == "image://playlistthumbs/virtual-media/4"


def test_pending_media_keeps_identity_and_disables_conflicting_actions() -> None:
    operation = MediaOperationRecord(
        operation_id="copy-1",
        scope_id="playlist:playlist-1",
        operation_type="copy",
        presentation=MediaOperationPresentation.TREE_LOCAL,
        state=MediaOperationState.COPYING,
        stage="Copying media…",
        completed=25,
        total=100,
        cancellable=True,
    )

    snapshot = PlaylistTreePresenter().build(
        _playlist(),
        revision=2,
        operations={"local-media": operation},
    )
    pending = snapshot.roots[0].children[0]
    roles = _roles(pending)

    assert pending.node_id == "local-media"
    assert pending.node_type.value == "media"
    assert roles["operationState"] == "copying"
    assert roles["operationProgress"] == 0.25
    assert roles["canDrag"] is False
    assert roles["canProject"] is False
    assert roles["canSetAsIdle"] is False


def test_presenter_uses_resolved_runtime_state_for_local_media() -> None:
    runtime = {
        "local-media": MediaPresentationState(
            availability=MediaAvailability.AVAILABLE,
            local_path="C:/media/local.mp4",
            thumbnail_source="image://playlistthumbs/local-media/1",
            duration_ticks=90 * 10_000_000,
        )
    }

    snapshot = PlaylistTreePresenter().build(
        _playlist(),
        revision=3,
        runtime_states=runtime,
    )
    roles = _roles(snapshot.roots[0].children[0])

    assert roles["duration"] == "1:30"
    assert roles["trimAvailable"] is True
    assert roles["isMissing"] is False
    assert roles["canSetAsIdle"] is True

    remote_roles = _roles(snapshot.roots[0].children[1])
    assert remote_roles["canSetAsIdle"] is False


def test_presenter_structurally_shares_unchanged_immutable_nodes() -> None:
    presenter = PlaylistTreePresenter()
    first = presenter.build(_playlist(), revision=1)
    changed_runtime = {
        "local-media": MediaPresentationState(
            availability=MediaAvailability.AVAILABLE,
            duration_ticks=30 * 10_000_000,
        )
    }

    second = presenter.build(
        _playlist(),
        revision=2,
        runtime_states=changed_runtime,
    )

    assert second.roots[0] is not first.roots[0]
    assert second.roots[0].children[0] is not first.roots[0].children[0]
    assert second.roots[0].children[1] is first.roots[0].children[1]
