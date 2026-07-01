from __future__ import annotations

from pathlib import Path

import pytest

from solin.core.meetings.meeting_folder_imports import (
    find_meeting_folder_import_record,
    is_meeting_folder_source_supported,
    make_meeting_folder_import_record,
    meeting_folder_matches_tree,
    same_local_source,
    target_section_code_for_pub_type,
    upsert_meeting_folder_import_record,
)


def test_meeting_folder_matching_uses_tag_pub_type_and_week():
    assert meeting_folder_matches_tree(
        {"monday": "2026-05-25", "meeting_tag": "MW"},
        tree_monday="2026-05-25",
        tree_pub_type="mwb",
    )
    assert not meeting_folder_matches_tree(
        {"monday": "2026-05-25", "meeting_tag": "WE"},
        tree_monday="2026-05-25",
        tree_pub_type="mwb",
    )


def test_target_section_code_is_meeting_type_policy():
    assert target_section_code_for_pub_type("mwb") == "lac"
    assert target_section_code_for_pub_type("wt") == "public_talk"
    assert target_section_code_for_pub_type("memorial") == ""


def test_source_support_respects_office_conversion_availability():
    assert is_meeting_folder_source_supported(
        {"kind": "media"},
        office_conversion_available=False,
    )
    assert not is_meeting_folder_source_supported(
        {"kind": "lo"},
        office_conversion_available=False,
    )
    assert is_meeting_folder_source_supported(
        {"kind": "lo"},
        office_conversion_available=True,
    )


def test_find_record_matches_same_path_across_source_keys(tmp_path: Path):
    path = str(tmp_path / "2026-05-27 MW" / "manual.mp4")
    records = {
        "old-machine-key": {
            "path": path,
            "status": "processed",
            "signature": {"size": 5, "mtime_ns": 123},
        }
    }

    record = find_meeting_folder_import_record(
        {"source_key": "new-machine-key", "path": path},
        records,
    )

    assert record is records["old-machine-key"]


def test_same_local_source_normalizes_local_paths(tmp_path: Path):
    source = tmp_path / "folder" / "manual.mp4"
    equivalent = tmp_path / "folder" / "." / "manual.mp4"

    assert same_local_source(str(source), str(equivalent))
    assert not same_local_source("https://example.test/a.mp4", str(source))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            r"C:\Meeting\Manual.mp4",
            r"c:/meeting/section/../manual.mp4",
        ),
        (
            r"\\MediaServer\Shared\Meeting\manual.mp4",
            r"\\mediaserver\shared\Meeting\.\manual.mp4",
        ),
    ],
)
def test_same_local_source_uses_windows_semantics_independent_of_host(
    left: str,
    right: str,
):
    assert same_local_source(left, right)


def test_same_local_source_keeps_windows_drives_distinct():
    assert not same_local_source(
        r"C:\Meeting\manual.mp4",
        r"D:\Meeting\manual.mp4",
    )


def test_upsert_record_replaces_same_path_records_and_truncates_errors():
    source = {
        "source_key": "new",
        "path": r"C:\Meeting\manual.mp4",
        "name": "manual.mp4",
        "kind": "media",
        "signature": {"size": 5, "mtime_ns": 123},
    }
    old_records = {
        "old": {
            "source_key": "old",
            "path": r"C:\Meeting\.\manual.mp4",
            "name": "manual.mp4",
            "kind": "media",
            "signature": {"size": 5, "mtime_ns": 100},
            "status": "processed",
            "node_ids": ["old-node"],
        }
    }

    record = make_meeting_folder_import_record(
        source,
        ["new-node"],
        status="failed",
        error="x" * 600,
    )
    updated = upsert_meeting_folder_import_record(old_records, record)

    assert list(updated) == ["new"]
    assert updated["new"]["node_ids"] == ["new-node"]
    assert updated["new"]["status"] == "failed"
    assert updated["new"]["error"] == "x" * 500
