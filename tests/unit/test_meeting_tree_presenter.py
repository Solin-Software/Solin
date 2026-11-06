from solin.ui.qml.media_tree.meeting_presenter import MeetingTreePresenter
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState


def _nodes() -> list[dict]:
    return [
        {
            "id": "section-1",
            "type": "section",
            "title": "Treasures",
            "color_hue": 215,
            "collapsed": False,
            "children": [
                {
                    "id": "media-1",
                    "type": "media",
                    "title": "Talk",
                    "media_type": "video",
                    "media_ref": {
                        "file_path": "C:/meeting/talk.mp4",
                        "mime_type": "video/mp4",
                        "label": "Talk",
                    },
                    "children": [],
                }
            ],
        }
    ]


def test_meeting_presenter_uses_the_shared_snapshot_contract() -> None:
    snapshot = MeetingTreePresenter(badge_provider=str.title).build(
        "midweek:2026-07-20",
        _nodes(),
        revision=8,
        runtime_states={
            "media-1": MediaPresentationState(
                availability=MediaAvailability.AVAILABLE,
                local_path="C:/meeting/talk.mp4",
                thumbnail_source="image://playlistthumbs/media-1/2",
            )
        },
    )

    section = snapshot.roots[0]
    media = section.children[0]
    assert snapshot.tree_id == "meeting:midweek:2026-07-20"
    assert section.thawed_roles()["itemCount"] == 1
    assert media.thawed_roles()["badge"] == "Video"
    assert media.thawed_roles()["thumbSource"] == "image://playlistthumbs/media-1/2"
    assert media.thawed_roles()["trimAvailable"] is True
    assert media.thawed_roles()["canSetAsIdle"] is True


def test_additional_count_changes_when_provenance_changes_without_topology_change():
    nodes = _nodes()
    presenter = MeetingTreePresenter()
    initial = presenter.build("meeting", nodes, revision=1)
    assert initial.roots[0].thawed_roles()["additionalMediaCount"] == 1
    nodes[0]["children"][0]["meeting_generated"] = True
    updated = presenter.build("meeting", nodes, revision=2)
    assert updated.roots[0].thawed_roles()["itemCount"] == 1
    assert updated.roots[0].thawed_roles()["additionalMediaCount"] == 0


def test_pending_meeting_import_does_not_enable_additional_media_removal():
    from solin.ui.qml.media_tree.meeting_presenter import PendingMeetingNodes

    nodes = _nodes()
    media = nodes[0]["children"].pop()
    snapshot = MeetingTreePresenter().build(
        "meeting", nodes, revision=1,
        pending_groups=(PendingMeetingNodes("pending", (media,), "section:section-1"),),
    )
    assert snapshot.roots[0].thawed_roles()["itemCount"] == 1
    assert snapshot.roots[0].thawed_roles()["additionalMediaCount"] == 0


def test_meeting_presenter_counts_offline_virtual_media() -> None:
    nodes = _nodes()
    nodes[0]["children"].append(
        {
            "id": "virtual-media",
            "type": "media",
            "media_ref": {
                "file_path": "https://example.test/video.mp4",
                "mime_type": "video/mp4",
            },
            "children": [],
        }
    )

    snapshot = MeetingTreePresenter().build("weekend:1", nodes, revision=1)

    assert snapshot.roots[0].thawed_roles()["itemCount"] == 2
    virtual = snapshot.roots[0].children[1]
    assert virtual.thawed_roles()["cloudVisible"] is True
    assert virtual.thawed_roles()["isMissing"] is False
    assert virtual.thawed_roles()["canSetAsIdle"] is False
