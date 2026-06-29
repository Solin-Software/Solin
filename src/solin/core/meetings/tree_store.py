"""
tree_store.py - Solin
=============================
Per-profile persistence for automatic meeting trees.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any

from solin.core.storage.json_repository import JsonFileRepository

from .thumbnails import meeting_thumb_cache_key, meeting_thumb_dir
from .tree_types import Node, clone_nodes, count_media, iter_nodes_strict

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MeetingTreeKey:
    """Structured identity encoded in a persisted meeting tree key."""

    pub_type: str
    monday: date
    language: str
    issue: str


@dataclass(frozen=True, slots=True)
class MeetingTreeOverview:
    """Small persisted summary used to render meeting cards without network I/O."""

    title: str = ""
    media_count: int = 0
    cover_bytes: bytes | None = None

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "title": self.title,
            "media_count": max(0, int(self.media_count)),
        }
        if self.cover_bytes:
            record["cover_b64"] = base64.b64encode(self.cover_bytes).decode("ascii")
        return record

    @classmethod
    def from_record(
        cls,
        value: object,
        *,
        fallback_media_count: int = 0,
    ) -> "MeetingTreeOverview":
        if not isinstance(value, dict):
            return cls(media_count=fallback_media_count)
        title = str(value.get("title") or "")
        media_count = _int_or_default(value.get("media_count"), fallback_media_count)
        return cls(
            title=title,
            media_count=max(0, media_count),
            cover_bytes=_decode_cover_bytes(value.get("cover_b64")),
        )


@dataclass(frozen=True, slots=True)
class MeetingTreeSnapshot:
    """Persisted meeting tree selected for a publication/week/language."""

    tree_key: str
    pub_type: str
    monday: date
    language: str
    issue: str
    nodes: list[Node]
    canonical_hash: str
    deleted_source_keys: set[str]
    linked_folder_files: dict[str, str]
    meeting_folder_imports: dict[str, dict[str, Any]]
    overview: MeetingTreeOverview

    @property
    def media_count(self) -> int:
        return count_media(self.nodes)


class MeetingTreeStore:
    """Load/save meeting trees in the active profile directory."""

    def __init__(
        self,
        path: str | Path,
    ) -> None:
        self._json = JsonFileRepository(path)

    @property
    def path(self) -> Path:
        return self._json.path

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "trees": {}}

    def load_all(self) -> dict[str, Any]:
        path = self.path
        if not self._json.exists():
            return self._empty()
        try:
            return self.load_all_strict()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning("Could not load meeting tree store %s: %s", path, exc)
            return self._empty()

    def load_all_strict(self) -> dict[str, Any]:
        """Load meeting trees while preserving read/parse failures for destructive callers."""
        if not self._json.exists():
            return self._empty()
        data = self._json.read()
        if not isinstance(data, dict):
            raise ValueError("Meeting tree storage root must be an object")
        data.setdefault("version", 1)
        data.setdefault("trees", {})
        if not isinstance(data["trees"], dict):
            raise ValueError("Meeting tree storage 'trees' must be an object")
        return data

    def load(self, tree_key: str) -> tuple[list[Node] | None, str]:
        record = self.load_all().get("trees", {}).get(tree_key)
        if not isinstance(record, dict):
            return None, ""
        nodes = record.get("nodes")
        if not isinstance(nodes, list):
            return None, ""
        return clone_nodes(nodes), str(record.get("last_canonical_hash", ""))

    def load_deleted_source_keys(self, tree_key: str) -> set[str]:
        record = self.load_all().get("trees", {}).get(tree_key)
        if not isinstance(record, dict):
            return set()
        values = record.get("deleted_source_keys", [])
        if not isinstance(values, list):
            return set()
        return {str(value) for value in values if value}

    def load_linked_folder_files(self, tree_key: str) -> dict[str, str]:
        """Return ``{file_path: node_id}`` for linked-folder items in *tree_key*."""
        record = self.load_all().get("trees", {}).get(tree_key)
        if not isinstance(record, dict):
            return {}
        mapping = record.get("linked_folder_files", {})
        if not isinstance(mapping, dict):
            return {}
        return {str(k): str(v) for k, v in mapping.items() if k and v}

    def load_meeting_folder_imports(self, tree_key: str) -> dict[str, dict[str, Any]]:
        """Return source-file import records for meeting-targeted folders."""
        record = self.load_all().get("trees", {}).get(tree_key)
        if not isinstance(record, dict):
            return {}
        mapping = record.get("meeting_folder_imports", {})
        if not isinstance(mapping, dict):
            return {}
        return {
            str(k): v
            for k, v in mapping.items()
            if k and isinstance(v, dict)
        }

    def find_snapshot(
        self,
        pub_type: str,
        monday: date,
        language: str,
    ) -> MeetingTreeSnapshot | None:
        """Return the newest usable persisted tree for publication/week/language."""
        pub = (pub_type or "").strip()
        lang = (language or "").strip()
        if not pub or not lang:
            return None
        prefix = f"{pub}:{monday.isoformat()}:{lang}:"
        trees = self.load_all().get("trees", {})
        if not isinstance(trees, dict):
            return None

        candidates: list[MeetingTreeSnapshot] = []
        for tree_key, record in trees.items():
            if not isinstance(tree_key, str) or not tree_key.startswith(prefix):
                continue
            snapshot = _snapshot_from_record(tree_key, record)
            if snapshot is not None and snapshot.media_count > 0:
                candidates.append(snapshot)
        if not candidates:
            return None
        return max(candidates, key=lambda snapshot: _issue_sort_key(snapshot.issue))

    def save(
        self,
        tree_key: str,
        nodes: list[Node],
        canonical_hash: str,
        deleted_source_keys: set[str] | None = None,
        linked_folder_files: dict[str, str] | None = None,
        meeting_folder_imports: dict[str, dict[str, Any]] | None = None,
        overview: MeetingTreeOverview | None = None,
    ) -> None:
        data = self.load_all()
        trees = data.setdefault("trees", {})
        existing = trees.get(tree_key)
        existing_overview = (
            existing.get("overview")
            if isinstance(existing, dict) and isinstance(existing.get("overview"), dict)
            else None
        )
        record: dict[str, Any] = {
            "deleted_source_keys": sorted(deleted_source_keys or set()),
            "last_canonical_hash": canonical_hash,
            "nodes": clone_nodes(nodes),
        }
        if overview is not None:
            record["overview"] = overview.to_record()
        elif existing_overview is not None:
            record["overview"] = existing_overview
        if linked_folder_files:
            record["linked_folder_files"] = dict(linked_folder_files)
        if meeting_folder_imports:
            record["meeting_folder_imports"] = dict(meeting_folder_imports)
        trees[tree_key] = record
        self._write(data)

    def remove_old_trees(self, keep: set[str]) -> None:
        if not keep:
            return
        data = self.load_all()
        trees = data.setdefault("trees", {})
        changed = False
        for key in list(trees.keys()):
            if key not in keep:
                trees.pop(key, None)
                changed = True
        if changed:
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self._json.write(data, sort_keys=True, trailing_newline=True)


def _stored_file_name(value: str) -> str:
    if "\\" in value:
        return PureWindowsPath(value).name
    return Path(value).name


def _int_or_default(value: object, default: int = 0) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_cover_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        return None


def parse_meeting_tree_key(tree_key: str) -> MeetingTreeKey | None:
    parts = tree_key.split(":", 3)
    if len(parts) != 4:
        return None
    pub_type, monday_text, language, issue = parts
    if not pub_type or not monday_text or not language:
        return None
    try:
        monday = date.fromisoformat(monday_text)
    except ValueError:
        return None
    return MeetingTreeKey(pub_type, monday, language, issue)


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if item}


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if k and v}


def _import_mapping(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(k): v
        for k, v in value.items()
        if k and isinstance(v, dict)
    }


def _snapshot_from_record(
    tree_key: str,
    record: object,
) -> MeetingTreeSnapshot | None:
    key = parse_meeting_tree_key(tree_key)
    if key is None or not isinstance(record, dict):
        return None
    nodes = record.get("nodes")
    if not isinstance(nodes, list):
        return None
    cloned_nodes = clone_nodes(nodes)
    media_count = count_media(cloned_nodes)
    overview = MeetingTreeOverview.from_record(
        record.get("overview"),
        fallback_media_count=media_count,
    )
    return MeetingTreeSnapshot(
        tree_key=tree_key,
        pub_type=key.pub_type,
        monday=key.monday,
        language=key.language,
        issue=key.issue,
        nodes=cloned_nodes,
        canonical_hash=str(record.get("last_canonical_hash", "")),
        deleted_source_keys=_string_set(record.get("deleted_source_keys", [])),
        linked_folder_files=_string_mapping(record.get("linked_folder_files", {})),
        meeting_folder_imports=_import_mapping(record.get("meeting_folder_imports", {})),
        overview=overview,
    )


def _issue_sort_key(issue: str) -> tuple[int, int | str, str]:
    if issue.isdigit():
        return (1, int(issue), issue)
    return (0, issue, issue)


def flush_meeting_thumbs_dir(
    *,
    store: MeetingTreeStore,
    thumb_dir: str | Path,
) -> None:
    """
    Remove cached meeting thumbnails no longer referenced by meeting_trees.json.

    Playlist thumbnails have their own cleanup path; meeting thumbnails live in
    cache/meeting_thumbs so meeting cleanup can use meeting tree state directly.
    """
    target_dir = meeting_thumb_dir(meeting_thumb_cache_dir=thumb_dir)
    if not target_dir.is_dir():
        return

    referenced: set[str] = set()
    try:
        data = store.load_all_strict()
        for tree_key, record in data.get("trees", {}).items():
            if not isinstance(record, dict):
                raise ValueError(f"Meeting tree '{tree_key}' must be an object")
            if "nodes" not in record:
                raise ValueError(f"Meeting tree '{tree_key}' must contain nodes")
            for node in iter_nodes_strict(
                record["nodes"],
                context=f"trees[{tree_key!r}].nodes",
            ):
                if node.get("type") != "media":
                    continue
                item_id = str(node.get("id", "") or "")
                if item_id:
                    referenced.add(meeting_thumb_cache_key(item_id))
                cache_key = str(node.get("thumbnail_cache_key") or "")
                if cache_key:
                    referenced.add(_stored_file_name(cache_key))
                local_path = str(node.get("thumbnail_local_path") or "")
                if local_path:
                    referenced.add(_stored_file_name(local_path))
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        log.warning(
            "Could not validate meeting thumbnail references from %s: %s",
            store.path,
            exc,
        )
        return

    for path in target_dir.iterdir():
        if path.name in referenced:
            continue
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
