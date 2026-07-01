"""Policies for auto-importing files from meeting-targeted linked folders."""

from __future__ import annotations

import ntpath
import os
from collections.abc import Mapping
from typing import Any

MEETING_FOLDER_TAG_PUB_TYPES = {
    "MW": "mwb",
    "WE": "wt",
}
MEETING_FOLDER_TARGET_SECTION_CODES = {
    "mwb": "lac",
    "wt": "public_talk",
}
DIRECT_MEETING_FOLDER_SOURCE_KINDS = frozenset(
    {"media", "pdf", "jwpub", "jwlplaylist"}
)
CONVERTIBLE_DOCUMENT_SOURCE_KIND = "lo"


def pub_type_for_meeting_tag(meeting_tag: str) -> str:
    return MEETING_FOLDER_TAG_PUB_TYPES.get(meeting_tag.upper(), "")


def meeting_folder_matches_tree(
    folder: Mapping[str, Any],
    *,
    tree_monday: str,
    tree_pub_type: str,
) -> bool:
    return (
        str(folder.get("monday") or "") == tree_monday
        and pub_type_for_meeting_tag(str(folder.get("meeting_tag") or ""))
        == tree_pub_type
    )


def target_section_code_for_pub_type(pub_type: str) -> str:
    return MEETING_FOLDER_TARGET_SECTION_CODES.get(pub_type, "")


def is_meeting_folder_source_supported(
    source: Mapping[str, Any],
    *,
    office_conversion_available: bool,
) -> bool:
    kind = str(source.get("kind") or "")
    return kind in DIRECT_MEETING_FOLDER_SOURCE_KINDS or (
        kind == CONVERTIBLE_DOCUMENT_SOURCE_KIND and office_conversion_available
    )


def same_local_source(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left.startswith(("http://", "https://")) or right.startswith(
        ("http://", "https://")
    ):
        return left == right
    return _normalized_local_path(left) == _normalized_local_path(right)


def find_meeting_folder_import_record(
    source: Mapping[str, Any],
    records: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    source_key = str(source.get("source_key") or "")
    record = records.get(source_key)
    if isinstance(record, dict):
        return record

    source_path = str(source.get("path") or "")
    if not source_path:
        return None
    for existing in records.values():
        if not isinstance(existing, dict):
            continue
        if same_local_source(str(existing.get("path") or ""), source_path):
            return existing
    return None


def make_meeting_folder_import_record(
    source: Mapping[str, Any],
    node_ids: list[str] | None = None,
    *,
    status: str = "processed",
    error: str = "",
) -> dict[str, Any]:
    source_key = str(source.get("source_key") or "")
    if not source_key:
        return {}
    record: dict[str, Any] = {
        "source_key": source_key,
        "path": str(source.get("path") or ""),
        "name": str(source.get("name") or ""),
        "kind": str(source.get("kind") or ""),
        "signature": dict(source.get("signature") or {}),
        "status": status,
        "node_ids": list(node_ids or []),
    }
    if error:
        record["error"] = str(error)[:500]
    return record


def upsert_meeting_folder_import_record(
    records: Mapping[str, dict[str, Any]],
    record: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    source_key = str(record.get("source_key") or "")
    if not source_key:
        return {str(key): dict(value) for key, value in records.items()}

    source_path = str(record.get("path") or "")
    updated: dict[str, dict[str, Any]] = {}
    for key, existing in records.items():
        if key == source_key:
            continue
        if source_path and same_local_source(str(existing.get("path") or ""), source_path):
            continue
        updated[str(key)] = dict(existing)
    updated[source_key] = dict(record)
    return updated


def _normalized_local_path(path: str) -> str:
    drive, _ = ntpath.splitdrive(path)
    if os.name == "nt" or drive:
        absolute_path = ntpath.abspath(path) if os.name == "nt" else path
        normalized = ntpath.normcase(ntpath.normpath(absolute_path))
        return f"windows:{normalized}"

    normalized = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return f"native:{normalized}"
