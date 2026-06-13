"""
tree_store.py - Solin
=============================
Per-profile persistence for automatic meeting trees.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path, PureWindowsPath
from typing import Any

from solin.core.storage.json_files import read_json_file, write_json_atomic

from .thumbnails import meeting_thumb_cache_key, meeting_thumb_dir
from .tree_types import Node, clone_nodes, iter_nodes

log = logging.getLogger(__name__)


class MeetingTreeStore:
    """Load/save meeting trees in the active profile directory."""

    def __init__(
        self,
        path: str | Path,
    ) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "trees": {}}

    def load_all(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return self._empty()
        try:
            data = read_json_file(path)
            if not isinstance(data, dict):
                return self._empty()
            data.setdefault("version", 1)
            data.setdefault("trees", {})
            if not isinstance(data["trees"], dict):
                data["trees"] = {}
            return data
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning("Could not load meeting tree store %s: %s", path, exc)
            return self._empty()

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

    def save(
        self,
        tree_key: str,
        nodes: list[Node],
        canonical_hash: str,
        deleted_source_keys: set[str] | None = None,
        linked_folder_files: dict[str, str] | None = None,
        meeting_folder_imports: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        data = self.load_all()
        record: dict[str, Any] = {
            "deleted_source_keys": sorted(deleted_source_keys or set()),
            "last_canonical_hash": canonical_hash,
            "nodes": clone_nodes(nodes),
        }
        if linked_folder_files:
            record["linked_folder_files"] = dict(linked_folder_files)
        if meeting_folder_imports:
            record["meeting_folder_imports"] = dict(meeting_folder_imports)
        data.setdefault("trees", {})[tree_key] = record
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
        path = self.path
        write_json_atomic(path, data, sort_keys=True, trailing_newline=True)


def _stored_file_name(value: str) -> str:
    if "\\" in value:
        return PureWindowsPath(value).name
    return Path(value).name


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
    data = store.load_all()
    for record in data.get("trees", {}).values():
        if not isinstance(record, dict):
            continue
        nodes = record.get("nodes", [])
        if not isinstance(nodes, list):
            continue
        for node in iter_nodes(nodes):
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

    for path in target_dir.iterdir():
        if path.name in referenced:
            continue
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
