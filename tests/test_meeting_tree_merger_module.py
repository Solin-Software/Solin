from __future__ import annotations

from app.core.meetings.tree_merger import MeetingTreeMerger, _media_identity_signature


def test_meeting_tree_merger_lives_in_core_module():
    assert MeetingTreeMerger.__module__ == "app.core.meetings.tree_merger"


def test_media_identity_signature_uses_media_ref_fields():
    node = {"media_ref": {"key_symbol": "mwb", "track": 12, "mime_type": "video/mp4"}}

    assert _media_identity_signature(node) == (
        "",
        "video/mp4",
        "",
        "mwb",
        "12",
        "",
        "",
    )


def test_media_identity_signature_ignores_file_path_for_jw_media():
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

    assert _media_identity_signature(stale) == _media_identity_signature(canonical)


def test_media_identity_signature_keeps_file_path_for_local_media():
    first = {"media_ref": {"mime_type": "video/mp4", "file_path": r"C:\media\one.mp4"}}
    second = {"media_ref": {"mime_type": "video/mp4", "file_path": r"C:\media\two.mp4"}}

    assert _media_identity_signature(first) != _media_identity_signature(second)
