import pytest

from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
    MediaTreeSnapshotError,
)


def _media(node_id: str, **roles) -> MediaTreeNodeSnapshot:
    return MediaTreeNodeSnapshot.create(
        node_id,
        MediaTreeNodeType.MEDIA,
        roles=roles,
    )


def test_snapshot_deep_freezes_role_data() -> None:
    framing = {"zoom": 1.2, "points": [0.25, 0.75]}
    node = _media("media-1", imageFraming=framing)

    framing["zoom"] = 9.0
    framing["points"].append(1.0)

    assert node.thawed_roles()["imageFraming"] == {
        "points": [0.25, 0.75],
        "zoom": 1.2,
    }


def test_snapshot_rejects_unknown_roles_and_runtime_objects() -> None:
    with pytest.raises(MediaTreeSnapshotError, match="Unknown media-tree roles"):
        _media("media-1", children=[])

    with pytest.raises(MediaTreeSnapshotError, match="object or null"):
        _media("media-1", imageFraming=object())


def test_snapshot_rejects_duplicate_ids_across_the_hierarchy() -> None:
    duplicate = _media("same-id")
    section = MediaTreeNodeSnapshot.create(
        "section-1",
        MediaTreeNodeType.SECTION,
        children=(duplicate,),
    )

    with pytest.raises(MediaTreeSnapshotError, match="Duplicate node ID"):
        MediaTreeSnapshot.create("playlist:1", 1, (section, _media("same-id")))


def test_snapshot_rejects_reusing_a_node_object() -> None:
    reused = _media("media-1")

    with pytest.raises(MediaTreeSnapshotError, match="reused or cyclic"):
        MediaTreeSnapshot.create("playlist:1", 1, (reused, reused))


def test_snapshot_rejects_invalid_hierarchy_and_role_types() -> None:
    invalid_section = MediaTreeNodeSnapshot.create(
        "nested-section",
        MediaTreeNodeType.SECTION,
    )
    parent = MediaTreeNodeSnapshot.create(
        "parent-section",
        MediaTreeNodeType.SECTION,
        children=(invalid_section,),
    )
    with pytest.raises(MediaTreeSnapshotError, match="cannot be inside"):
        MediaTreeSnapshot.create("playlist:1", 1, (parent,))

    with pytest.raises(MediaTreeSnapshotError, match="must be a boolean"):
        _media("media-1", isMissing="no")
    with pytest.raises(MediaTreeSnapshotError, match="between -1 and 1"):
        _media("media-1", operationProgress=float("nan"))


@pytest.mark.parametrize("revision", [-1, True, 1.5])
def test_snapshot_revision_must_be_a_non_negative_integer(revision) -> None:
    with pytest.raises(MediaTreeSnapshotError, match="revision"):
        MediaTreeSnapshot.create("playlist:1", revision)
