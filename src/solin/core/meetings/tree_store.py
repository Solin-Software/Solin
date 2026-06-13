"""
tree_store.py - Solin
=============================
Per-profile persistence for automatic meeting trees.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from solin.core.foundation import paths as _paths
from solin.core.profiles.manager import ProfileManager

from .tree_types import Node, clone_nodes, iter_nodes

log = logging.getLogger(__name__)

_FILENAME = "meeting_trees.json"


class MeetingTreeStore:
    """Load/save meeting trees in the active profile directory."""

    def __init__(self, profile_id: str = "") -> None:
        self._pm = ProfileManager()
        self._profile_id = profile_id

    @property
    def path(self) -> Path:
        if hasattr(self._pm, "meeting_trees_file"):
            return Path(self._pm.meeting_trees_file(self._profile_id))
        return self._pm.profile_dir(self._profile_id) / _FILENAME

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "trees": {}}

    def load_all(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return self._empty()
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)


def flush_meeting_thumbs_dir(profile_id: str = "") -> None:
    """
    Remove cached meeting thumbnails no longer referenced by meeting_trees.json.

    Playlist thumbnails have their own cleanup path; meeting thumbnails live in
    cache/meeting_thumbs so meeting cleanup can use meeting tree state directly.
    """
    thumb_dir = getattr(_paths, "MEETING_THUMB_CACHE_DIR", "")
    if not thumb_dir or not os.path.isdir(thumb_dir):
        return

    referenced: set[str] = set()
    data = MeetingTreeStore(profile_id).load_all()
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
                referenced.add(f"{item_id}.jpg")
            cache_key = str(node.get("thumbnail_cache_key") or "")
            if cache_key:
                referenced.add(os.path.basename(cache_key))
            local_path = str(node.get("thumbnail_local_path") or "")
            if local_path:
                referenced.add(os.path.basename(local_path))

    for fname in os.listdir(thumb_dir):
        if fname not in referenced:
            path = os.path.join(thumb_dir, fname)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
