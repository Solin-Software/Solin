from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from solin.core.meetings.canonical_restore import canonical_source_keys
from solin.core.meetings.models import MeetingMedia, MeetingPublicationRef, WeekData
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_merger import (
    MeetingTreeMerger,
    media_identity_signature,
    merge_persisted_meeting_trees,
)
from solin.core.meetings.tree_store import MeetingTreeOverview, MeetingTreeStore
from solin.core.meetings.tree_types import iter_nodes, source_hash


def test_meeting_tree_merger_lives_in_core_module():
    assert MeetingTreeMerger.__module__ == "solin.core.meetings.tree_merger"


@pytest.mark.parametrize("complete", [False, True])
def test_partial_merge_updates_healthy_nodes_without_removing_unavailable_nodes(complete):
    def media(key, title):
        return {
            "id": key, "type": "media", "meeting_generated": True,
            "meeting_source_key": key, "title": title, "children": [],
        }

    saved = [
        {"id": "manual", "type": "media", "title": "Operator", "children": []},
        media("healthy", "Old"), media("unavailable", "Keep until confirmed"),
        media("deleted", "Explicitly deleted"),
    ]
    canonical = [media("healthy", "New"), media("added", "New reference")]
    merged = MeetingTreeMerger(
        canonical, {"deleted"}, preserve_missing=not complete
    ).merge(saved)
    by_id = {node["id"]: node for node in merged}
    assert by_id["healthy"]["title"] == "New"
    assert by_id["added"]["title"] == "New reference"
    assert by_id["manual"]["title"] == "Operator"
    assert ("unavailable" in by_id) is (not complete)
    assert "deleted" not in by_id


@pytest.mark.parametrize("manual_first", [False, True])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("source_section", ["lac", "tgw"])
def test_renamed_publication_keeps_moved_media_once_without_empty_wrappers(
    manual_first, complete, source_section
):
    builder = MeetingTreeBuilder()

    def canonical(publication_title, media_title):
        return builder.build_midweek(
            WeekData(
                monday=date(2026, 5, 25),
                mwb_publication_refs=[
                    MeetingPublicationRef(
                        pub="book",
                        section=source_section,
                        publication_title=publication_title,
                        caption="Study",
                        meps_doc_id=8,
                        is_cbs=source_section == "lac",
                        items=[
                            MeetingMedia(
                                multimedia_id=1,
                                mime_type="video/mp4",
                                key_symbol="book",
                                track=1,
                                label=media_title,
                            )
                        ],
                    )
                ],
            )
        )

    saved = canonical("Original publication", "Original media")
    incoming = canonical("Renamed publication", "Updated media")
    section = next(node for node in saved if node["meeting_source_key"] == f"section:mwb:{source_section}")
    publication = section["children"][0]
    moved = next(node for node in publication["children"] if node["type"] == "media")
    publication["children"].remove(moved)
    moved["start_trim_ticks"] = 42_000
    manual = {
        "id": "operator-group",
        "type": "subsection",
        "title": "Operator group",
        "children": [
            moved,
            {"id": "operator-note", "type": "marker", "text": "Keep this note", "children": []},
        ],
    }
    if source_section == "lac":
        section["children"] = [manual, publication] if manual_first else [publication, manual]
    else:
        target = next(node for node in saved if node["meeting_source_key"] == "section:mwb:lac")
        target["children"].append(manual)
        if manual_first:
            saved.reverse()

    merged = MeetingTreeMerger(incoming, preserve_missing=True).merge(saved)
    if complete:
        merged = MeetingTreeMerger(incoming).merge(merged)

    nodes = list(iter_nodes(merged))
    source_counts = Counter(
        node["meeting_source_key"] for node in nodes if node.get("meeting_generated")
    )
    assert all(count == 1 for count in source_counts.values()), source_counts
    assert len({node["id"] for node in nodes}) == len(nodes)
    media = [node for node in nodes if node["type"] == "media"]
    assert len(media) == 1
    assert media[0]["title"] == "Updated media"
    assert media[0]["start_trim_ticks"] == 42_000
    operator_group = next(node for node in nodes if node["id"] == "operator-group")
    assert media[0] in operator_group["children"]
    assert sum(node["id"] == "operator-note" for node in nodes) == 1
    assert all(
        node["children"]
        for node in nodes
        if node["type"] == "subsection" and node.get("meeting_generated")
    ), "A renamed publication must not leave an empty generated wrapper"


@pytest.mark.parametrize("different_issue", [False, True])
def test_new_partial_media_stays_hidden_after_restart_retry_and_completion(tmp_path, different_issue):
    def media(key, title):
        return {
            "id": key,
            "type": "media",
            "meeting_generated": True,
            "meeting_source_key": key,
            "title": title,
            "media_ref": {"mime_type": "image/jpeg", "file_path": str(tmp_path / f"{key}.jpg")},
            "children": [],
        }

    path = tmp_path / "meeting_trees.json"
    store = MeetingTreeStore(path)
    old_key = "mwb:2026-05-25:T:20260500"
    target_key = "mwb:2026-05-25:T:20260700" if different_issue else old_key
    old_base = media("base", "Original base")
    unavailable = media("unavailable", "Previously loaded reference")
    manual = {"id": "operator-note", "type": "marker", "text": "Keep this note", "children": []}
    old_dependencies = [
        {"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "parent-old"},
        {"pub": "book", "language": "T", "issue": "0", "checksum": "book-old"},
    ]
    original = store.save(
        old_key,
        [old_base, unavailable, manual],
        source_hash([old_base, unavailable]),
        canonical_nodes=[old_base, unavailable],
        source_checksum="parent-old",
        publication_dependencies=old_dependencies,
    )
    added = media("new-healthy", "New healthy reference")
    incoming = [media("base", "Updated base"), added]
    new_dependencies = [
        {
            "pub": "mwb",
            "language": "T",
            "issue": "20260700" if different_issue else "20260500",
            "checksum": "parent-new",
        }
    ]
    partial = store.reconcile(
        target_key,
        incoming,
        source_hash(incoming),
        MeetingTreeOverview(title="Updated meeting"),
        fallback=original,
        canonical_complete=False,
        source_checksum="parent-new",
        publication_dependencies=new_dependencies,
    )
    assert partial.canonical_hash == ""
    assert canonical_source_keys(partial.canonical_nodes) == {"base", "unavailable", "new-healthy"}
    assert partial.source_checksum == "parent-old"
    assert partial.publication_dependencies == old_dependencies
    assert all(node["id"] != "operator-note" for node in iter_nodes(partial.canonical_nodes))

    # Save the same baseline, tombstone and hidden-node state as the tree controller.
    store.save(
        target_key,
        [node for node in partial.nodes if node.get("meeting_source_key") != "new-healthy"],
        partial.canonical_hash,
        deleted_source_keys={"new-healthy"},
        canonical_nodes=partial.canonical_nodes,
        hidden_canonical_media={"new-healthy": deepcopy(added)},
    )
    restarted = MeetingTreeStore(path)
    hidden = restarted.snapshot(target_key)
    assert hidden is not None
    assert hidden.deleted_source_keys == {"new-healthy"}
    assert hidden.hidden_canonical_media["new-healthy"] == added

    for complete in (False, True):
        result = restarted.reconcile(
            target_key,
            incoming,
            source_hash(incoming),
            MeetingTreeOverview(title="Updated meeting"),
            canonical_complete=complete,
            source_checksum="parent-new",
            publication_dependencies=new_dependencies,
        )
        visible = {node["id"]: node for node in iter_nodes(result.nodes)}
        assert "new-healthy" not in visible
        assert visible["base"]["title"] == "Updated base"
        assert "operator-note" in visible
        assert ("unavailable" in visible) is (not complete)
        assert result.deleted_source_keys == {"new-healthy"}
        assert result.hidden_canonical_media["new-healthy"] == added
        assert bool(result.canonical_hash) is complete
        assert result.source_checksum == ("parent-new" if complete else "parent-old")
        assert result.publication_dependencies == (new_dependencies if complete else old_dependencies)

    persisted = MeetingTreeStore(path).snapshot(target_key)
    assert persisted is not None
    assert "new-healthy" not in {node["id"] for node in iter_nodes(persisted.nodes)}
    assert persisted.hidden_canonical_media["new-healthy"] == added


def testmedia_identity_signature_uses_media_ref_fields():
    node = {"media_ref": {"key_symbol": "mwb", "track": 12, "mime_type": "video/mp4"}}

    assert media_identity_signature(node) == (
        "",
        "video/mp4",
        "",
        "mwb",
        "12",
        "",
        "",
    )


def testmedia_identity_signature_ignores_file_path_for_jw_media():
    stale = {
        "media_ref": {
            "multimedia_id": 15,
            "mime_type": "video/mp4",
            "file_path": "w_LGP_202604_02_r720P.mp4",
            "key_symbol": "w",
            "track": 2,
            "issue_tag": 20260400,
            "meps_doc_id": 2026365,
        }
    }
    canonical = {
        "media_ref": {
            "multimedia_id": 15,
            "mime_type": "video/mp4",
            "file_path": "",
            "key_symbol": "w",
            "track": 2,
            "issue_tag": 20260400,
            "meps_doc_id": 2026365,
        }
    }

    assert media_identity_signature(stale) == media_identity_signature(canonical)


def testmedia_identity_signature_keeps_file_path_for_local_media():
    first = {"media_ref": {"mime_type": "video/mp4", "file_path": r"C:\media\one.mp4"}}
    second = {"media_ref": {"mime_type": "video/mp4", "file_path": r"C:\media\two.mp4"}}

    assert media_identity_signature(first) != media_identity_signature(second)


def test_merger_preserves_prepared_framing_for_same_generated_media() -> None:
    canonical = [{
        "id": "canonical-id",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:1",
        "media_ref": {"mime_type": "image/png", "file_path": "slide.png"},
        "children": [],
    }]
    saved = [{
        **canonical[0],
        "id": "saved-id",
        "image_framing": {
            "version": 1,
            "zoom": 1.4,
            "norm_x": 0.12,
            "norm_y": -0.08,
        },
    }]

    merged = MeetingTreeMerger(canonical).merge(saved)

    assert merged[0]["id"] == "saved-id"
    assert merged[0]["image_framing"] == saved[0]["image_framing"]


def test_merger_drops_prepared_framing_when_media_identity_changes() -> None:
    canonical = [{
        "id": "canonical-id",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:1",
        "media_ref": {"mime_type": "image/png", "file_path": "new-slide.png"},
        "children": [],
    }]
    saved = [{
        **canonical[0],
        "media_ref": {"mime_type": "image/png", "file_path": "old-slide.png"},
        "image_framing": {
            "version": 1,
            "zoom": 1.4,
            "norm_x": 0.12,
            "norm_y": -0.08,
        },
    }]

    merged = MeetingTreeMerger(canonical).merge(saved)

    assert "image_framing" not in merged[0]


def test_portable_merge_keeps_fresh_prepared_duration_for_official_media() -> None:
    prepared = [{
        "id": "official-local",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:official",
        "media_ref": {"key_symbol": "mwbv", "track": 1},
        "base_duration_ticks": 120_000_000,
        "children": [],
    }]
    portable = [{
        **prepared[0],
        "id": "official-portable",
        "base_duration_ticks": 60_000_000,
        "start_trim_ticks": 10_000,
    }]

    merged = merge_persisted_meeting_trees(prepared, portable)

    assert merged[0]["base_duration_ticks"] == 120_000_000
    assert merged[0]["start_trim_ticks"] == 10_000


def test_portable_merge_keeps_fresh_prepared_duration_for_manual_media() -> None:
    prepared = [{
        "id": "manual",
        "type": "media",
        "meeting_generated": False,
        "media_ref": {"file_path": "clip.mp4", "mime_type": "video/mp4"},
        "base_duration_ticks": 120_000_000,
        "children": [],
    }]
    portable = [{
        **prepared[0],
        "base_duration_ticks": 60_000_000,
        "end_trim_ticks": 10_000,
    }]

    merged = merge_persisted_meeting_trees(prepared, portable)

    assert merged[0]["base_duration_ticks"] == 120_000_000
    assert merged[0]["end_trim_ticks"] == 10_000
