from __future__ import annotations

from solin.core.meetings.tree_merger import (
    MeetingTreeMerger,
    media_identity_signature,
    merge_persisted_meeting_trees,
)


def test_meeting_tree_merger_lives_in_core_module():
    assert MeetingTreeMerger.__module__ == "solin.core.meetings.tree_merger"


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
