from __future__ import annotations

from solin.ui.qml.media_tree.model import MediaTreeSource, SnapshotPublishResult
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)


def _media(node_id: str, *, revision: int = 0, title: str = ""):
    return MediaTreeNodeSnapshot.create(
        node_id,
        MediaTreeNodeType.MEDIA,
        source_revision=revision,
        roles={"title": title, "mediaType": "video", "url": ""},
    )


def _snapshot(revision: int, *roots: MediaTreeNodeSnapshot) -> MediaTreeSnapshot:
    return MediaTreeSnapshot.create("playlist:test", revision, roots)


def test_source_projects_a_deeply_detached_complete_tree() -> None:
    media = _media("media-1", title="Song")
    section = MediaTreeNodeSnapshot.create(
        "section-1",
        MediaTreeNodeType.SECTION,
        roles={"title": "Opening", "collapsed": False, "itemCount": 1},
        children=(media,),
    )
    source = MediaTreeSource("playlist:test")

    assert source.publish_snapshot(_snapshot(1, section)) == SnapshotPublishResult.PUBLISHED

    first_projection = source.treeData
    first_projection[0]["title"] = "Mutated outside the source"
    first_projection[0]["children"][0]["title"] = "Also mutated"
    next_projection = source.treeData
    assert next_projection[0]["title"] == "Opening"
    assert next_projection[0]["children"][0]["title"] == "Song"
    assert source.mediaCount == 1


def test_source_coalesces_complete_snapshots_during_visual_interaction() -> None:
    source = MediaTreeSource("playlist:test")
    source.publish_snapshot(_snapshot(1, _media("a", title="A"), _media("b", title="B")))
    published: list[int] = []
    source.snapshotPublished.connect(lambda _tree_id, revision: published.append(revision))

    source.beginInteraction()
    assert source.publish_snapshot(
        _snapshot(2, _media("b", title="B"), _media("a", title="A2"))
    ) == SnapshotPublishResult.DEFERRED
    assert source.publish_snapshot(
        _snapshot(3, _media("b", title="B3"), _media("a", title="A3"))
    ) == SnapshotPublishResult.DEFERRED

    assert [node["id"] for node in source.treeData] == ["a", "b"]
    source.endInteraction()
    assert [node["id"] for node in source.treeData] == ["b", "a"]
    assert source.revision == 3
    assert published == [3]


def test_source_rejects_stale_foreign_and_regressed_node_state() -> None:
    source = MediaTreeSource("playlist:test")
    assert source.publish_snapshot(_snapshot(2, _media("media-1", revision=2))) == (
        SnapshotPublishResult.PUBLISHED
    )
    assert source.publish_snapshot(_snapshot(1, _media("stale"))) == (
        SnapshotPublishResult.STALE
    )
    foreign = MediaTreeSnapshot.create("meeting:test", 3, (_media("wrong"),))
    assert source.publish_snapshot(foreign) == SnapshotPublishResult.TREE_MISMATCH

    try:
        source.publish_snapshot(_snapshot(3, _media("media-1", revision=1)))
    except ValueError as exc:
        assert "source revision regressed" in str(exc)
    else:
        raise AssertionError("A node source revision must never move backwards")


def test_source_rejects_type_changes_for_stable_ids() -> None:
    source = MediaTreeSource("playlist:test")
    source.publish_snapshot(_snapshot(1, _media("stable")))
    changed = MediaTreeNodeSnapshot.create(
        "stable",
        MediaTreeNodeType.SECTION,
        roles={"title": "", "collapsed": False, "itemCount": 0},
    )

    try:
        source.publish_snapshot(_snapshot(2, changed))
    except ValueError as exc:
        assert "changed type" in str(exc)
    else:
        raise AssertionError("A stable node ID must keep its type")


def test_source_switches_tree_without_replacing_its_qobject_identity() -> None:
    source = MediaTreeSource("playlist:first")
    source.publish_snapshot(
        MediaTreeSnapshot.create("playlist:first", 1, (_media("first"),))
    )

    result = source.activate_snapshot(
        MediaTreeSnapshot.create(
            "playlist:second",
            7,
            (_media("second"), _media("third")),
        )
    )

    assert result == SnapshotPublishResult.PUBLISHED
    assert source.treeId == "playlist:second"
    assert source.revision == 7
    assert source.mediaCount == 2
    assert [node["id"] for node in source.treeData] == ["second", "third"]


def test_transition_keeps_the_previous_tree_until_atomic_activation() -> None:
    source = MediaTreeSource("playlist:first")
    source.publish_snapshot(
        MediaTreeSnapshot.create("playlist:first", 1, (_media("old"),))
    )

    source.begin_transition("playlist:second")

    assert source.transitioning is True
    assert source.treeId == "playlist:first"
    assert [node["id"] for node in source.treeData] == ["old"]

    source.activate_snapshot(
        MediaTreeSnapshot.create("playlist:second", 2, (_media("new"),))
    )

    assert source.transitioning is False
    assert source.treeId == "playlist:second"
    assert [node["id"] for node in source.treeData] == ["new"]


def test_same_tree_reactivation_advances_existing_node_presentation_revision() -> None:
    source = MediaTreeSource("playlist:test")
    source.publish_snapshot(_snapshot(1, _media("same", title="Old")))
    first_revision = source.treeData[0]["presentationRevision"]

    source.begin_transition("playlist:test")
    source.activate_snapshot(_snapshot(2, _media("same", title="New")))

    node = source.treeData[0]
    assert node["title"] == "New"
    assert node["presentationRevision"] > first_revision


def test_topology_mutation_discards_a_snapshot_deferred_during_drag() -> None:
    source = MediaTreeSource("playlist:test")
    source.publish_snapshot(_snapshot(1, _media("a"), _media("b")))
    source.beginInteraction()
    assert source.publish_snapshot(
        _snapshot(2, _media("a", title="stale"), _media("b"))
    ) == SnapshotPublishResult.DEFERRED

    source.invalidate_pending_snapshot()
    source.endInteraction()

    assert source.revision == 1
    assert [node["id"] for node in source.treeData] == ["a", "b"]


def test_failed_transition_is_visible_retryable_and_never_stays_locked() -> None:
    source = MediaTreeSource("playlist:first")
    retries: list[bool] = []
    source.retryRequested.connect(lambda: retries.append(True))
    source.begin_transition("playlist:second")

    source.activate_error("playlist:second", 1, "invalid snapshot")

    assert source.treeId == "playlist:second"
    assert source.transitioning is False
    assert source.error == "invalid snapshot"
    assert source.treeData == []

    source.retry()

    assert source.transitioning is True
    assert source.error == ""
    assert retries == [True]
