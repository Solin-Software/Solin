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
