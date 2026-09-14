from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.meetings.canonical_restore import (
    canonical_tree_diff,
    restore_canonical_tree,
)
from solin.core.meetings.tree_store import MeetingTreeOverview, MeetingTreeStore
from solin.core.meetings.tree_types import iter_nodes, stable_node_id
from solin.core.meetings.models import MeetingMedia, MeetingPublicationRef, WeekData
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_merger import MeetingTreeMerger
from solin.ui.qml.media_tree.meeting_presenter import MeetingTreePresenter
from solin.ui.qml.media_tree.media_presenter import MediaRoleInput, media_roles
from solin.ui.qml.media_tree.state import MediaPresentationState
from solin.widgets.meetings.tree_controller import MeetingTreeController
from PySide6.QtWidgets import QMessageBox


def _multi_edition_week() -> WeekData:
    return WeekData(
        mwb_publication_refs=[
            MeetingPublicationRef(
                section="tgw",
                pub="w",
                issue=str(year * 10000 + 100),
                publication_title=f"Study Journal {year}",
                caption=f"Article {year}",
                meps_doc_id=year,
                begin_ordinal=index + 1,
                items=[
                    MeetingMedia(multimedia_id=1, label=f"Image {year}", mime_type="image/jpeg")
                ],
            )
            for index, year in enumerate((2021, 2025))
        ]
    )


def test_multi_edition_initial_merge_does_not_offer_restore() -> None:
    builder = MeetingTreeBuilder()
    canonical = builder.build_midweek(_multi_edition_week())
    initial = builder.build_midweek(WeekData())
    current = MeetingTreeMerger(canonical).merge(initial)
    assert not canonical_tree_diff(canonical, current).has_changes
    current = MeetingTreeMerger(canonical).merge(current)

    assert not canonical_tree_diff(canonical, current).has_changes
    assert [node["title"] for node in current[0]["children"]] == [
        "Study Journal 2021",
        "Study Journal 2025",
    ]


def test_multi_edition_restore_can_be_presented_after_reopening(tmp_path: Path) -> None:
    builder = MeetingTreeBuilder()
    canonical = builder.build_midweek(_multi_edition_week())
    current = MeetingTreeMerger(canonical).merge(builder.build_midweek(WeekData()))
    restored = restore_canonical_tree(canonical, current)
    path = tmp_path / "meeting_trees.json"
    key = "mwb:2026-09-07:T:20260900"
    MeetingTreeStore(path).save(
        key, restored, builder.canonical_hash(canonical), canonical_nodes=canonical
    )
    snapshot = MeetingTreeStore(path).snapshot(key)
    assert snapshot is not None
    MeetingTreePresenter().build(key, snapshot.nodes, revision=1)


def _legacy_multi_edition_tree() -> list[dict]:
    nodes = MeetingTreeBuilder().build_midweek(_multi_edition_week())
    for node in nodes[0]["children"]:
        node["meeting_source_key"] = "subsection:ref:tgw:w"
        node["id"] = stable_node_id("subsection:ref:tgw:w")
    return nodes


@pytest.mark.parametrize("merged", [False, True])
def test_legacy_multi_edition_store_recovers_offline(tmp_path: Path, merged: bool) -> None:
    canonical = _legacy_multi_edition_tree()
    current = deepcopy(canonical)
    if merged:
        first, second = current[0]["children"]
        second["children"] = first["children"] + second["children"]
        current[0]["children"] = [second]
    key = "mwb:2026-09-07:T:20260900"
    path = tmp_path / "meeting_trees.json"
    path.write_text(
        json.dumps(
            {
                "version": 5,
                "trees": {
                    key: {
                        "nodes": current,
                        "canonical_nodes": canonical,
                        "last_canonical_hash": "old",
                        "canonical_reset_generation": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = MeetingTreeStore(path)

    assert store.migrate_strict()
    snapshot = store.snapshot(key)
    assert snapshot is not None
    assert not canonical_tree_diff(snapshot.canonical_nodes, snapshot.nodes).has_changes
    assert snapshot.canonical_reset_generation == 2
    assert [node["title"] for node in snapshot.nodes[0]["children"]] == [
        "Study Journal 2021",
        "Study Journal 2025",
    ]
    MeetingTreePresenter().build(key, snapshot.nodes, revision=1)
    assert not MeetingTreeStore(path).migrate_strict()
    assert MeetingTreeStore(path).snapshot(key) == snapshot


def test_legacy_migration_preserves_user_content_and_outside_moves() -> None:
    from solin.core.meetings.tree_migrations import migrate_publication_subsections

    canonical = _legacy_multi_edition_tree()
    current = deepcopy(canonical)
    first, second = current[0]["children"]
    first["title"] = "My edition"
    first["user_title_override"] = True
    first["collapsed"] = False
    first["color_hue"] = 42
    manual = _manual_media("manual", "My video")
    first["children"].append(manual)
    moved = second["children"].pop()
    moved["start_trim_ticks"] = 120
    moved["resolved_url"] = "https://example.invalid/media.mp4"
    current[2]["children"].append(moved)
    deleted = {"media:missing"}

    assert migrate_publication_subsections(current, canonical, deleted)
    first = current[0]["children"][0]
    assert first["title"] == "My edition"
    assert first["user_title_override"]
    assert first["color_hue"] == 42
    assert not first["collapsed"]
    assert first["children"][-1] == manual
    assert current[2]["children"] == [moved]
    assert deleted == {"media:missing"}
    MeetingTreePresenter().build("meeting", current, revision=1)
    assert not migrate_publication_subsections(current, canonical, deleted)


def test_legacy_subsection_deletion_maps_to_both_editions() -> None:
    from solin.core.meetings.tree_migrations import migrate_publication_subsections

    canonical = _legacy_multi_edition_tree()
    current = deepcopy(canonical)
    current[0]["children"] = []
    deleted = {"subsection:ref:tgw:w"}
    migrate_publication_subsections(current, canonical, deleted)

    assert deleted == {node["meeting_source_key"] for node in canonical[0]["children"]}
    assert current[0]["children"] == []


def test_legacy_collapsed_groups_recover_around_intervening_media() -> None:
    from solin.core.meetings.tree_migrations import migrate_publication_subsections

    canonical = _legacy_multi_edition_tree()
    intervening = _media("media:between", "Between editions")
    canonical[0]["children"].insert(1, intervening)
    current = deepcopy(canonical)
    first, between, second = current[0]["children"]
    second["children"] = first["children"] + second["children"]
    manual = _manual_media("manual", "My video")
    current[0]["children"] = [manual, second, between]

    migrate_publication_subsections(current, canonical, set())

    assert not canonical_tree_diff(canonical, current).has_changes
    assert current[0]["children"][0] == manual
    assert current[0]["children"][2] == intervening


@pytest.mark.parametrize("sync_generation", [0, 1, 2])
def test_legacy_sync_cannot_reintroduce_duplicate_groups(
    tmp_path: Path,
    sync_generation: int,
) -> None:
    canonical = MeetingTreeBuilder().build_midweek(_multi_edition_week())
    snapshot = MeetingTreeStore(tmp_path / "meeting_trees.json").save(
        "mwb:2026-09-07:T:20260900",
        canonical,
        "hash",
        canonical_nodes=canonical,
        canonical_reset_generation=1,
    )
    remote = _legacy_multi_edition_tree()
    manual = _manual_media("manual", "My video")
    remote[0]["children"].append(manual)
    original_remote = deepcopy(remote)
    controller = SimpleNamespace(_canonical_nodes=canonical)

    result = MeetingTreeController._merge_snapshot_with_sync(
        controller,
        snapshot,
        remote,
        set(),
        sync_generation,
        {},
    )

    assert not canonical_tree_diff(canonical, result).has_changes
    assert next(node for node in iter_nodes(result) if node["id"] == "manual") == manual
    MeetingTreePresenter().build(snapshot.tree_key, result, revision=1)
    assert remote == original_remote


def test_publication_group_identity_is_stable_during_enrichment() -> None:
    builder = MeetingTreeBuilder()
    week = _multi_edition_week()
    initial = builder.build_midweek(week)
    week.mwb_publication_refs.reverse()
    for ref in week.mwb_publication_refs:
        ref.publication_title = "  " + ref.publication_title.replace(" ", "   ") + " "
        ref.items.append(MeetingMedia(multimedia_id=2, mime_type="image/jpeg"))
    enriched = builder.build_midweek(week)
    assert [node["id"] for node in initial[0]["children"]] == [
        node["id"] for node in enriched[0]["children"]
    ]


def test_cbs_fallback_and_publication_references_share_group_identity() -> None:
    builder = MeetingTreeBuilder()
    item = MeetingMedia(multimedia_id=1, cbs_article_title="Article", mime_type="image/jpeg")
    week = WeekData(
        cbs_ref={
            "pub": "book",
            "publication_title": "Study Book",
            "doc_titles": {42: "Article"},
        },
        cbs_items=[item],
    )
    fallback = builder.build_midweek(week)
    week.mwb_publication_refs = [
        MeetingPublicationRef(
            section="lac",
            pub="book",
            publication_title="Study Book",
            caption="Article",
            meps_doc_id=42,
            is_cbs=True,
            items=[item],
        )
    ]
    prepared = builder.build_midweek(week)
    assert fallback[2]["children"][0]["id"] == prepared[2]["children"][0]["id"]
    assert not canonical_tree_diff(prepared, fallback).has_changes


def _section(
    key: str,
    title: str,
    children: list[dict],
    *,
    node_id: str | None = None,
    hue: int = 210,
) -> dict:
    return {
        "id": node_id or f"id-{key}",
        "type": "section",
        "title": title,
        "color_hue": hue,
        "collapsed": False,
        "children": children,
        "meeting_generated": True,
        "meeting_source_key": key,
    }


def _subsection(
    key: str,
    title: str,
    children: list[dict],
    *,
    node_id: str | None = None,
    hue: int = 220,
) -> dict:
    return {
        "id": node_id or f"id-{key}",
        "type": "subsection",
        "title": title,
        "color_hue": hue,
        "collapsed": False,
        "children": children,
        "meeting_generated": True,
        "meeting_source_key": key,
    }


def _marker(key: str, text: str, *, node_id: str | None = None) -> dict:
    return {
        "id": node_id or f"id-{key}",
        "type": "marker",
        "text": text,
        "children": [],
        "meeting_generated": True,
        "meeting_source_key": key,
    }


def _media(key: str, title: str, *, node_id: str | None = None) -> dict:
    return {
        "id": node_id or f"id-{key}",
        "type": "media",
        "title": title,
        "children": [],
        "media_ref": {
            "file_path": f"{key}.mp4",
            "mime_type": "video/mp4",
        },
        "meeting_generated": True,
        "meeting_source_key": key,
    }


def _manual_media(node_id: str, title: str) -> dict:
    return {
        "id": node_id,
        "type": "media",
        "title": title,
        "children": [],
        "media_ref": {
            "file_path": f"{node_id}.mp4",
            "mime_type": "video/mp4",
        },
        "meeting_generated": False,
    }


def _by_key(nodes: list[dict]) -> dict[str, dict]:
    return {
        str(node["meeting_source_key"]): node
        for node in iter_nodes(nodes)
        if node.get("meeting_source_key")
    }


def _by_id(nodes: list[dict]) -> dict[str, dict]:
    return {str(node["id"]): node for node in iter_nodes(nodes)}


def test_diff_ignores_manual_nodes_and_operational_media_state() -> None:
    canonical = [
        _section(
            "section:mwb:lac",
            "Our Christian Life",
            [
                _media("media:a", "A"),
                _media("media:b", "B"),
            ],
        )
    ]
    current = [
        _manual_media("manual-root", "Root"),
        canonical[0]
        | {
            "collapsed": True,
            "children": [
                _manual_media("manual-2", "Manual 2"),
                canonical[0]["children"][0]
                | {
                    "resolved_url": "https://example.invalid/a.mp4",
                    "thumbnail_cache_key": "thumb",
                    "base_duration_ticks": 90_000,
                    "start_trim_ticks": 3_000,
                    "end_trim_ticks": 4_000,
                    "image_framing": {"zoom": 1.25},
                },
                _manual_media("manual-1", "Manual 1"),
                canonical[0]["children"][1],
            ],
        },
    ]

    assert not canonical_tree_diff(canonical, current).has_changes


def test_diff_detects_redundant_official_title_override() -> None:
    canonical = [_section("section:mwb:lac", "LIVING AS CHRISTIANS", [])]
    current = [canonical[0] | {"user_title_override": True}]

    diff = canonical_tree_diff(canonical, current)

    assert diff.renamed == 1


def test_diff_detects_every_official_divergence_category() -> None:
    canonical = [
        _section(
            "section:a",
            "Section A",
            [
                _subsection(
                    "sub:a",
                    "Subsection",
                    [
                        _marker("marker:a", "Marker"),
                        _media("media:a", "Media A")
                        | {"resolved_url": "https://example.invalid/canonical-a.mp4"},
                    ],
                ),
                _media("media:b", "Media B"),
            ],
        ),
        _section("section:b", "Section B", [_media("media:c", "Media C")]),
    ]
    changed = _by_key(canonical)
    current = [
        changed["section:b"]
        | {
            "children": [
                changed["media:a"]
                | {
                    "title": "Custom media",
                    "user_title_override": True,
                },
                changed["media:c"],
            ],
        },
        changed["section:a"]
        | {
            "title": "Renamed section",
            "color_hue": 12,
            "children": [
                changed["media:b"],
                changed["sub:a"]
                | {
                    "title": "Renamed subsection",
                    "children": [
                        changed["marker:a"]
                        | {
                            "text": "Renamed marker",
                            "user_title_override": True,
                        },
                    ],
                },
            ],
        },
        _media("media:stale", "Stale"),
    ]

    diff = canonical_tree_diff(canonical, current, {"media:c"})

    assert diff.hidden == 1
    assert diff.renamed == 4
    assert diff.recolored == 1
    assert diff.moved > 0
    assert diff.stale == 1


def test_restore_rebuilds_official_tree_and_preserves_manual_state() -> None:
    canonical = [
        _section(
            "section:a",
            "Section A",
            [
                _subsection(
                    "sub:a",
                    "Subsection",
                    [
                        _marker("marker:a", "Marker"),
                        _media("media:a", "Media A")
                        | {"resolved_url": "https://example.invalid/canonical-a.mp4"},
                    ],
                ),
                _media("media:b", "Media B"),
            ],
            hue=205,
        ),
        _section("section:b", "Section B", [_media("media:c", "Media C")]),
    ]
    by_key = _by_key(canonical)
    moved_media = by_key["media:a"] | {
        "id": "persisted-media-a",
        "title": "Accidental title",
        "user_title_override": True,
        "resolved_url": "C:/cache/a.mp4",
        "thumbnail_local_path": "C:/cache/a.jpg",
        "thumbnail_cache_key": "a.jpg",
        "base_duration_ticks": 123_000,
        "start_trim_ticks": 3_000,
        "end_trim_ticks": 7_000,
        "image_framing": {"version": 1, "zoom": 1.4},
    }
    manual_marker = {
        "id": "manual-marker",
        "type": "marker",
        "text": "Manual marker",
        "children": [],
        "meeting_generated": False,
    }
    manual_subsection = {
        "id": "manual-sub",
        "type": "subsection",
        "title": "Manual subsection",
        "color_hue": 44,
        "collapsed": True,
        "children": [manual_marker, _manual_media("manual-sub-media", "Sub media")],
        "meeting_generated": False,
    }
    manual_root_section = {
        "id": "manual-root-section",
        "type": "section",
        "title": "Manual root section",
        "color_hue": 88,
        "collapsed": True,
        "children": [_manual_media("manual-root-media", "Root media")],
        "meeting_generated": False,
    }
    current = [
        manual_root_section,
        by_key["section:b"]
        | {
            "children": [
                _manual_media("manual-other-section", "Other section"),
                moved_media,
                by_key["media:c"],
            ],
        },
        by_key["section:a"]
        | {
            "id": "persisted-section-a",
            "title": "Accidental name",
            "color_hue": 7,
            "collapsed": True,
            "children": [
                by_key["media:b"],
                _manual_media("manual-a", "Manual A"),
                manual_subsection,
                by_key["sub:a"]
                | {
                    "id": "persisted-sub-a",
                    "title": "Accidental subsection",
                    "children": [
                        by_key["marker:a"]
                        | {
                            "id": "persisted-marker-a",
                            "text": "Accidental marker",
                            "user_title_override": True,
                        },
                    ],
                },
            ],
        },
    ]

    restored = restore_canonical_tree(canonical, current)
    restored_by_key = _by_key(restored)
    restored_by_id = _by_id(restored)

    assert [
        node.get("meeting_source_key") for node in restored if node.get("meeting_generated")
    ] == [
        "section:a",
        "section:b",
    ]
    assert [node["id"] for node in restored] == [
        "persisted-section-a",
        "manual-root-section",
        by_key["section:b"]["id"],
    ]
    section_a = restored_by_key["section:a"]
    assert section_a["title"] == "Section A"
    assert section_a["color_hue"] == 205
    assert section_a["collapsed"] is True
    assert restored_by_key["sub:a"]["title"] == "Subsection"
    assert restored_by_key["marker:a"]["text"] == "Marker"
    assert restored_by_key["marker:a"]["id"] == "persisted-marker-a"

    media_a = restored_by_key["media:a"]
    assert media_a["id"] == "persisted-media-a"
    assert media_a["title"] == "Media A"
    assert "user_title_override" not in media_a
    assert media_a["resolved_url"] == "C:/cache/a.mp4"
    assert media_a["thumbnail_local_path"] == "C:/cache/a.jpg"
    assert media_a["start_trim_ticks"] == 3_000
    assert media_a["end_trim_ticks"] == 7_000
    assert media_a["image_framing"] == {"version": 1, "zoom": 1.4}

    assert restored_by_id["manual-sub"]["collapsed"] is True
    assert restored_by_id["manual-marker"]["text"] == "Manual marker"
    section_b_ids = [child["id"] for child in restored_by_key["section:b"]["children"]]
    assert section_b_ids == ["manual-other-section", by_key["media:c"]["id"]]
    assert restored_by_id["manual-a"]["title"] == "Manual A"


def test_restore_uses_folder_fallback_only_for_orphaned_imports() -> None:
    canonical = [_section("section:mwb:lac", "Our Christian Life", [])]
    orphan = _manual_media("orphan", "Orphan") | {
        "linked_folder_source": "C:/Meetings/2026-05-25 MW"
    }
    root_media = _manual_media("root-media", "Root") | {
        "linked_folder_source": "C:/Meetings/2026-05-25 MW"
    }
    materialized_orphan = _manual_media("materialized-orphan", "Materialized") | {
        "linked_folder_source": "C:/Meetings/2026-05-25 MW"
    }
    stale_parent = _section(
        "section:stale",
        "Old section",
        [orphan, materialized_orphan],
    )

    restored = restore_canonical_tree(
        canonical,
        [root_media, stale_parent],
        orphan_section_source_key="section:mwb:lac",
        orphan_media_ids={"orphan", "root-media"},
    )

    assert [node["id"] for node in restored[-2:]] == [
        "root-media",
        "materialized-orphan",
    ]
    lac = _by_key(restored)["section:mwb:lac"]
    assert [child["id"] for child in lac["children"]] == ["orphan"]


def test_restore_never_inverts_manual_siblings_when_official_anchors_cross() -> None:
    media_a = _media("media:a", "A")
    media_b = _media("media:b", "B")
    media_c = _media("media:c", "C")
    canonical = [_section("section:a", "Section", [media_c, media_a, media_b])]
    current = [
        _section(
            "section:a",
            "Section",
            [
                media_a,
                _manual_media("manual-1", "Manual 1"),
                media_b,
                _manual_media("manual-2", "Manual 2"),
                media_c,
            ],
        )
    ]

    restored = restore_canonical_tree(canonical, current)
    children = restored[0]["children"]
    official_order = [
        child["meeting_source_key"] for child in children if child.get("meeting_generated")
    ]
    manual_order = [child["id"] for child in children if not child.get("meeting_generated")]

    assert official_order == ["media:c", "media:a", "media:b"]
    assert manual_order == ["manual-1", "manual-2"]


def test_restore_recovers_hidden_media_title_and_remote_download_url() -> None:
    canonical_media = _media("media:hidden", "Media") | {"auto_title": True}
    canonical = [_section("section:a", "Section", [canonical_media])]
    hidden_media = canonical_media | {
        "id": "persisted-hidden-id",
        "title": "Resolved official title",
        "auto_title": False,
        "resolved_url": "https://cdn.example.invalid/official.mp4",
        "thumbnail_url": "https://cdn.example.invalid/official.jpg",
        "base_duration_ticks": 123_000,
    }

    restored = restore_canonical_tree(
        canonical,
        [],
        hidden_canonical_media={"media:hidden": hidden_media},
    )
    media = _by_key(restored)["media:hidden"]

    assert media["id"] == "persisted-hidden-id"
    assert media["title"] == "Resolved official title"
    assert media["auto_title"] is False
    assert media["resolved_url"] == "https://cdn.example.invalid/official.mp4"
    assert media["thumbnail_url"] == "https://cdn.example.invalid/official.jpg"
    assert media["base_duration_ticks"] == 123_000

    roles = media_roles(
        MediaRoleInput(
            node_id=media["id"],
            title=media["title"],
            media_type="video",
            badge="Video",
            url=media["resolved_url"],
        ),
        MediaPresentationState(),
        None,
    )
    assert roles["cloudVisible"] is True
    assert roles["cloudActive"] is False


def test_legacy_hidden_media_without_state_is_resolved_after_restore() -> None:
    class Service:
        calls: list[tuple] = []

        def resolve_media_async(self, request_id, media) -> None:
            self.calls.append((request_id, media))

    class Controller:
        _resolve_to_node_id: dict[str, str] = {}
        _resolved_urls: dict[str, str] = {}
        _svc = Service()

        def _url_for_node(self, node):
            return MeetingTreeController._url_for_node(self, node)

        def _request_jw_resolution(self, node, *, allow_generated=False):
            return MeetingTreeController._request_jw_resolution(
                self,
                node,
                allow_generated=allow_generated,
            )

    restored_media = _media("media:legacy", "Media") | {
        "media_ref": {
            "file_path": "",
            "mime_type": "video/mp4",
            "key_symbol": "mwbv",
            "track": 1,
        }
    }
    controller = Controller()

    MeetingTreeController._resolve_unresolved_canonical_media(
        controller,
        [restored_media],
    )

    assert len(controller._svc.calls) == 1
    request_id, media = controller._svc.calls[0]
    assert controller._resolve_to_node_id[request_id] == restored_media["id"]
    assert media.key_symbol == "mwbv"


def test_restored_placeholder_title_is_resolved_even_when_remote_url_survived() -> None:
    class Service:
        calls: list[tuple] = []

        def resolve_media_async(self, request_id, media) -> None:
            self.calls.append((request_id, media))

    class Controller:
        _resolve_to_node_id: dict[str, str] = {}
        _resolved_urls: dict[str, str] = {}
        _svc = Service()

        def _url_for_node(self, node):
            return MeetingTreeController._url_for_node(self, node)

        def _request_jw_resolution(self, node, *, allow_generated=False):
            return MeetingTreeController._request_jw_resolution(
                self,
                node,
                allow_generated=allow_generated,
            )

    restored_media = _media("media:renamed", "Media") | {
        "auto_title": True,
        "resolved_url": "https://cdn.example.invalid/official.mp4",
        "media_ref": {
            "file_path": "",
            "mime_type": "video/mp4",
            "key_symbol": "mwbv",
            "track": 2,
        },
    }
    controller = Controller()

    MeetingTreeController._resolve_unresolved_canonical_media(
        controller,
        [restored_media],
    )

    assert len(controller._svc.calls) == 1
    request_id, media = controller._svc.calls[0]
    assert controller._resolve_to_node_id[request_id] == restored_media["id"]
    assert media.track == 2


def test_store_persists_baseline_and_legacy_waits_for_reconciliation(tmp_path: Path) -> None:
    tree_key = "mwb:2026-05-25:T:issue"
    path = tmp_path / "meeting_trees.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "trees": {
                    tree_key: {
                        "nodes": [_manual_media("legacy", "Legacy")],
                        "last_canonical_hash": "legacy-hash",
                        "deleted_source_keys": ["media:a", "media:gone"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = MeetingTreeStore(path)

    legacy = store.snapshot(tree_key)
    assert legacy is not None
    assert legacy.canonical_nodes == []
    assert legacy.canonical_reset_generation == 0
    assert legacy.hidden_canonical_media == {}
    assert not canonical_tree_diff(legacy.canonical_nodes, legacy.nodes).has_changes

    canonical = [_section("section:a", "Section", [_media("media:a", "Media")])]
    reconciled = store.reconcile(
        tree_key,
        canonical,
        "canonical-hash",
        MeetingTreeOverview(title="Meeting", media_count=1),
    )
    assert reconciled.canonical_nodes == canonical
    assert reconciled.deleted_source_keys == {"media:a"}

    saved = store.save(
        tree_key,
        reconciled.nodes,
        reconciled.canonical_hash,
        {"media:a", "media:gone"},
        hidden_canonical_media={
            "media:a": _media("media:a", "Resolved")
            | {"resolved_url": "https://cdn.example.invalid/a.mp4"},
            "media:gone": _media("media:gone", "Gone"),
        },
    )
    assert saved.canonical_nodes == canonical
    assert saved.canonical_reset_generation == 0
    assert saved.deleted_source_keys == {"media:a"}
    assert set(saved.hidden_canonical_media) == {"media:a"}
    assert saved.hidden_canonical_media["media:a"]["title"] == "Resolved"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 6


def test_preliminary_reconciliation_preserves_confirmed_canonical_state(
    tmp_path: Path,
) -> None:
    tree_key = "mwb:2026-05-25:T:issue"
    store = MeetingTreeStore(tmp_path / "meeting_trees.json")
    complete = [_section("section:a", "Section", [_media("media:a", "Media")])]
    initial = store.reconcile(
        tree_key,
        complete,
        "complete-hash",
        MeetingTreeOverview(title="Meeting", media_count=1),
        source_checksum="complete-checksum",
    )
    store.save(
        tree_key,
        initial.nodes,
        initial.canonical_hash,
        {"media:a"},
    )

    preliminary = [_section("section:a", "Section", [])]
    pending = store.reconcile(
        tree_key,
        preliminary,
        "preliminary-hash",
        MeetingTreeOverview(title="Meeting", media_count=0),
        source_checksum="new-checksum",
        canonical_complete=False,
    )

    assert pending.canonical_nodes == complete
    assert pending.canonical_hash == "complete-hash"
    assert pending.source_checksum == "complete-checksum"
    assert pending.deleted_source_keys == {"media:a"}


def test_controller_exposes_canonical_restore_qml_contract() -> None:
    meta = MeetingTreeController.staticMetaObject

    assert meta.indexOfProperty("canonicalRestoreAvailable") >= 0
    assert meta.indexOfSignal("canonicalStateChanged()") >= 0
    assert meta.indexOfSlot("restoreCanonicalContent()") >= 0


class _Signal:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, *args) -> None:
        self.calls.append(args)


def _transaction_controller(store, *, sync_service=None):
    class FakeController:
        pass

    canonical = [_section("section:a", "Section", [_media("media:a", "Media")])]
    controller = FakeController()
    controller._canonical_nodes = canonical
    controller._nodes = []
    controller._deleted_source_keys = {"media:a"}
    controller._linked_folder_files = {}
    controller._meeting_folder_imports = {}
    controller._canonical_reset_generation = 5
    controller._hidden_canonical_media = {}
    controller._canonical_restore_available = True
    controller._meeting_type = "mwb"
    controller._tree_key = "mwb:2026-05-25:T:issue"
    controller._canonical_hash = "hash"
    controller._overview = None
    controller._store = store
    controller._sync_enabled = sync_service is not None
    controller._sync_folder = "C:/Meetings/2026-05-25 MW" if sync_service else ""
    controller._sync_service = sync_service
    controller._canonical_diff = lambda: canonical_tree_diff(
        controller._canonical_nodes,
        controller._nodes,
        controller._deleted_source_keys,
    )
    controller.parent = lambda: None
    controller.storageSaveFailed = _Signal()
    controller.storageSaved = _Signal()
    controller.canonicalStateChanged = _Signal()
    controller.chromeChanged = _Signal()
    controller.stateChanged = _Signal()
    controller._start_media_requests = lambda: None
    controller._emit_section_counts = lambda: None
    controller._generated_asset_roots = lambda: []
    controller._meeting_folder_imported_node_ids = lambda: set()
    controller.saved = 0
    controller._save_local_cache = lambda: (
        setattr(controller, "saved", controller.saved + 1) or True
    )

    def submit_modal_operation(**options):
        try:
            value = options["runner"](lambda _progress: None, CancellationFlag())
            options["commit"](value)
        except Exception as exc:  # noqa: BLE001 - test operation boundary
            options["failed"](str(exc))
        return True

    controller._submit_modal_operation = submit_modal_operation
    return controller


def test_restore_confirmation_is_concise_and_does_not_list_diff_categories() -> None:
    controller = _transaction_controller(object())
    captured: dict[str, str] = {}

    def reject(_parent, title, message, _buttons):
        captured["title"] = title
        captured["message"] = message
        return QMessageBox.StandardButton.No

    with patch.object(QMessageBox, "question", side_effect=reject):
        MeetingTreeController.restoreCanonicalContent(controller)

    assert captured["title"] == "Restore official meeting content"
    assert captured["message"] == (
        "Restore the official meeting content?\n\n"
        "Manually added content, trims, framing and expanded state will be kept."
    )
    assert "%1" not in captured["message"]
    assert "items" not in captured["message"]


def test_restore_commits_state_and_queues_persistence() -> None:
    controller = _transaction_controller(object())

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        MeetingTreeController.restoreCanonicalContent(controller)

    assert controller._nodes == controller._canonical_nodes
    assert controller._deleted_source_keys == set()
    assert controller._canonical_reset_generation == 6
    assert controller.saved == 1
    assert controller.storageSaved.calls == []
    assert controller.storageSaveFailed.calls == []


def test_restore_materialization_failure_never_persists_or_publishes() -> None:
    class Store:
        calls = 0

        def save(self, *_args, **_kwargs):
            self.calls += 1

    class FailingSync:
        def materialize_tree_files(self, *_args, **_kwargs):
            raise OSError("copy failed")

    store = Store()
    controller = _transaction_controller(store, sync_service=FailingSync())
    previous_nodes = controller._nodes

    with patch.object(
        QMessageBox,
        "question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        MeetingTreeController.restoreCanonicalContent(controller)

    assert controller._nodes is previous_nodes
    assert controller._deleted_source_keys == {"media:a"}
    assert store.calls == 0
    assert controller.saved == 0
    assert controller.storageSaved.calls == []
    assert controller.storageSaveFailed.calls == [("mwb:2026-05-25:T:issue", "copy failed")]


def test_deleting_official_section_with_manual_structure_is_blocked() -> None:
    class FakeController:
        pass

    manual_subsection = {
        "id": "manual-subsection",
        "type": "subsection",
        "title": "Manual",
        "children": [],
        "meeting_generated": False,
    }
    section = _section("section:a", "Official", [manual_subsection])
    controller = FakeController()
    controller._nodes = [section]
    controller._deleted_source_keys = set()
    controller._find_node = lambda node_id: next(
        (node for node in iter_nodes(controller._nodes) if node["id"] == node_id),
        None,
    )
    controller.parent = lambda: None

    with (
        patch.object(QMessageBox, "warning") as warning,
        patch.object(QMessageBox, "question") as question,
    ):
        MeetingTreeController.deleteSection(controller, section["id"])

    warning.assert_called_once()
    question.assert_not_called()
    assert controller._nodes == [section]
    assert controller._deleted_source_keys == set()
