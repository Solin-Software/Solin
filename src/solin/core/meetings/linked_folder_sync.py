"""Shared linked-folder sync for meeting trees."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from solin.core.ingest.manifest import (
    CACHE_DIR_NAME,
    MANIFEST_FILE,
    MANIFEST_LOCK,
    ManifestError,
    cache_dir,
    from_manifest_url,
    load_manifest,
    save_manifest,
    to_manifest_url,
)

from .folder_matcher import match_meeting_folder
from .schedule import UNCONFIGURED_WEEKDAY
from .schedule_settings import MeetingScheduleSettingsStore
from .tree_types import Node, clean_dict, clone_nodes, iter_nodes

log = logging.getLogger(__name__)

MEETING_TREE_KEY = "meeting_tree"
MEETING_TREE_SCHEMA_VERSION = 1


class MeetingSyncError(RuntimeError):
    """Raised when meeting linked-folder sync cannot complete safely."""


@dataclass(frozen=True, slots=True)
class MeetingSyncIdentity:
    tree_key: str
    pub_type: str
    monday: date
    canonical_hash: str = ""

    @property
    def meeting_tag(self) -> str:
        return meeting_tag_for_pub_type(self.pub_type)


@dataclass(frozen=True, slots=True)
class MeetingSyncRecord:
    folder: Path
    tree_key: str
    pub_type: str
    monday: date
    meeting_tag: str
    folder_date: date
    canonical_hash: str
    nodes: list[Node]
    deleted_source_keys: set[str]
    linked_folder_files: dict[str, str]
    meeting_folder_imports: dict[str, dict[str, Any]]
    revision: int


def meeting_tag_for_pub_type(pub_type: str) -> str:
    if pub_type == "mwb":
        return "MW"
    if pub_type == "wt":
        return "WE"
    raise MeetingSyncError(f"Meeting sync is not supported for '{pub_type}'.")


class MeetingLinkedFolderSync:
    """Domain service for meeting state stored in a watched meeting folder."""

    def __init__(self, schedule_settings: MeetingScheduleSettingsStore) -> None:
        self._schedule_settings = schedule_settings

    def folder_date_for(self, monday: date, pub_type: str) -> date:
        weekday = self._configured_weekday(pub_type)
        if weekday == UNCONFIGURED_WEEKDAY:
            return monday
        return monday + timedelta(days=weekday)

    def folder_name_for(self, identity: MeetingSyncIdentity) -> str:
        folder_date = self.folder_date_for(identity.monday, identity.pub_type)
        return f"{folder_date.isoformat()} {identity.meeting_tag}"

    def locate_folder(
        self,
        watched_root: str,
        identity: MeetingSyncIdentity,
        *,
        create: bool = False,
    ) -> Path | None:
        root = Path(watched_root)
        if not root.is_dir():
            if create:
                raise MeetingSyncError("Linked folder is not available.")
            return None

        candidates: list[Path] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            match = match_meeting_folder(child.name)
            if not match:
                continue
            if match.monday == identity.monday and match.meeting_tag == identity.meeting_tag:
                candidates.append(child)

        desired_name = self.folder_name_for(identity)
        if candidates:
            return min(
                candidates,
                key=lambda folder: (
                    0 if folder.name == desired_name else 1,
                    0 if self.manifest_matches(folder, identity) else 1,
                    folder.name.lower(),
                ),
            )

        if not create:
            return None

        folder = root / desired_name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def manifest_matches(self, folder: Path, identity: MeetingSyncIdentity) -> bool:
        try:
            block = load_manifest(folder, strict=True).get(MEETING_TREE_KEY)
        except ManifestError:
            return False
        return self._block_matches(block, identity)

    def load_tree(
        self,
        watched_root: str,
        identity: MeetingSyncIdentity,
    ) -> MeetingSyncRecord | None:
        folder = self.locate_folder(watched_root, identity, create=False)
        if folder is None:
            return None
        manifest = load_manifest(folder, strict=True)
        block = manifest.get(MEETING_TREE_KEY)
        if not self._block_matches(block, identity):
            return None
        assert isinstance(block, dict)
        return self._record_from_block(folder, block, identity)

    def save_tree(
        self,
        folder: Path,
        identity: MeetingSyncIdentity,
        *,
        nodes: list[Node],
        deleted_source_keys: set[str],
        linked_folder_files: dict[str, str],
        meeting_folder_imports: dict[str, dict[str, Any]],
        expected_revision: int,
    ) -> MeetingSyncRecord:
        with MANIFEST_LOCK:
            manifest = load_manifest(folder, strict=True)
            existing = manifest.get(MEETING_TREE_KEY)
            existing_revision = self._revision(existing)
            save_nodes = clone_nodes(nodes)
            save_deleted = set(deleted_source_keys)
            save_linked = dict(linked_folder_files)
            save_imports = {
                str(key): dict(value)
                for key, value in meeting_folder_imports.items()
                if isinstance(value, dict)
            }

            if (
                isinstance(existing, dict)
                and self._block_matches(existing, identity)
                and existing_revision > expected_revision
            ):
                existing_record = self._record_from_block(folder, existing, identity)
                save_nodes = self._merge_conflicting_nodes(existing_record.nodes, save_nodes)
                save_deleted |= existing_record.deleted_source_keys
                save_linked = {**existing_record.linked_folder_files, **save_linked}
                save_imports = {**existing_record.meeting_folder_imports, **save_imports}

            revision = max(existing_revision, expected_revision) + 1
            saved_block = self._block_from_tree(
                folder,
                identity,
                nodes=save_nodes,
                deleted_source_keys=save_deleted,
                linked_folder_files=save_linked,
                meeting_folder_imports=save_imports,
                revision=revision,
            )
            manifest[MEETING_TREE_KEY] = saved_block
            if not save_manifest(folder, manifest):
                raise MeetingSyncError(f"Could not write {MANIFEST_FILE}.")
            return self._record_from_block(folder, saved_block, identity)

    def delete_sync_metadata(self, folder: Path) -> None:
        manifest_path = folder / MANIFEST_FILE
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError as exc:
            raise MeetingSyncError(f"Could not remove {MANIFEST_FILE}.") from exc

        cache_path = folder / CACHE_DIR_NAME
        if cache_path.exists():
            try:
                shutil.rmtree(cache_path)
            except OSError:
                log.warning("Could not remove meeting sync cache %s", cache_path, exc_info=True)

    def materialize_tree_files(
        self,
        nodes: list[Node],
        folder: Path,
        *,
        generated_roots: Iterable[str],
    ) -> tuple[list[Node], dict[str, str]]:
        copied: dict[tuple[str, str], Path] = {}
        materialized = clone_nodes(nodes)
        linked_files: dict[str, str] = {}
        roots = [Path(root) for root in generated_roots if root]

        for node in iter_nodes(materialized):
            if node.get("type") != "media":
                continue
            node["linked_folder_source"] = str(folder)
            node_id = str(node.get("id") or "")
            for owner, field in self._local_url_fields(node):
                source = str(owner.get(field) or "")
                if not self._copyable_local_url(source):
                    continue
                source_path = Path(source)
                dest_dir = self._target_dir_for_node_file(
                    node,
                    field,
                    source_path,
                    folder,
                    roots,
                )
                if self._is_in_target_location(source_path, folder, dest_dir):
                    dest_path = source_path
                else:
                    cache_key = (
                        os.path.normcase(os.path.normpath(os.path.abspath(source))),
                        str(dest_dir),
                    )
                    dest_path = copied.get(cache_key)
                    if dest_path is None:
                        dest_path = self._copy_into_directory(source_path, dest_dir)
                        copied[cache_key] = dest_path
                owner[field] = str(dest_path)
                if node_id and field != "thumbnail_local_path":
                    linked_files[str(dest_path)] = node_id
        return materialized, linked_files

    def detach_cache_references(
        self,
        nodes: list[Node],
        folder: Path,
        durable_dir: Path,
    ) -> list[Node]:
        detached = clone_nodes(nodes)
        sync_cache = folder / CACHE_DIR_NAME
        durable_dir.mkdir(parents=True, exist_ok=True)

        for node in iter_nodes(detached):
            if node.get("type") != "media":
                node.pop("linked_folder_source", None)
                continue
            for owner, field in self._local_url_fields(node):
                value = str(owner.get(field) or "")
                if not value or value.startswith(("http://", "https://")):
                    continue
                path = Path(value)
                if not self._is_inside(path, sync_cache):
                    continue
                if path.is_file():
                    owner[field] = str(self._copy_into_directory(path, durable_dir))
                    continue
                if field == "thumbnail_local_path":
                    owner.pop(field, None)
                    node.pop("thumbnail_cache_key", None)
                else:
                    owner[field] = ""
            if self._primary_media_file_is_direct_child(node, folder):
                node["linked_folder_source"] = str(folder)
            else:
                node.pop("linked_folder_source", None)
        return detached

    def _configured_weekday(self, pub_type: str) -> int:
        schedule = self._schedule_settings.load()
        slot = schedule.midweek if pub_type == "mwb" else schedule.weekend
        if 0 <= slot.weekday <= 6:
            return slot.weekday
        return UNCONFIGURED_WEEKDAY

    def _block_matches(
        self,
        block: Any,
        identity: MeetingSyncIdentity,
    ) -> bool:
        if not isinstance(block, dict):
            return False
        if str(block.get("tree_key") or "") == identity.tree_key:
            return True
        return (
            str(block.get("pub_type") or "") == identity.pub_type
            and str(block.get("monday") or "") == identity.monday.isoformat()
            and str(block.get("meeting_tag") or "") == identity.meeting_tag
        )

    def _record_from_block(
        self,
        folder: Path,
        block: dict[str, Any],
        identity: MeetingSyncIdentity,
    ) -> MeetingSyncRecord:
        raw_nodes = block.get("nodes", [])
        nodes = self._resolve_nodes(raw_nodes if isinstance(raw_nodes, list) else [], folder)
        imports = self._resolve_imports(block.get("meeting_folder_imports", {}), folder)
        linked = self._resolve_linked_files(block.get("linked_folder_files", {}), folder)
        folder_date = self._date_from_block_or_folder(block, folder, identity)
        deleted_values = block.get("deleted_source_keys", [])
        deleted = set()
        if isinstance(deleted_values, list):
            deleted = {str(value) for value in deleted_values if value}
        return MeetingSyncRecord(
            folder=folder,
            tree_key=str(block.get("tree_key") or identity.tree_key),
            pub_type=str(block.get("pub_type") or identity.pub_type),
            monday=identity.monday,
            meeting_tag=str(block.get("meeting_tag") or identity.meeting_tag),
            folder_date=folder_date,
            canonical_hash=str(block.get("canonical_hash") or ""),
            nodes=nodes,
            deleted_source_keys=deleted,
            linked_folder_files=linked,
            meeting_folder_imports=imports,
            revision=self._revision(block),
        )

    def _block_from_tree(
        self,
        folder: Path,
        identity: MeetingSyncIdentity,
        *,
        nodes: list[Node],
        deleted_source_keys: set[str],
        linked_folder_files: dict[str, str],
        meeting_folder_imports: dict[str, dict[str, Any]],
        revision: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": MEETING_TREE_SCHEMA_VERSION,
            "tree_key": identity.tree_key,
            "pub_type": identity.pub_type,
            "monday": identity.monday.isoformat(),
            "meeting_tag": identity.meeting_tag,
            "folder_date": self._date_from_folder(folder, identity).isoformat(),
            "canonical_hash": identity.canonical_hash,
            "nodes": self._portable_nodes(nodes, folder),
            "deleted_source_keys": sorted(deleted_source_keys),
            "meeting_folder_imports": self._portable_imports(meeting_folder_imports, folder),
            "linked_folder_files": self._portable_linked_files(linked_folder_files, folder),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "revision": revision,
        }

    def _portable_nodes(self, nodes: list[Node], folder: Path) -> list[Node]:
        result = clone_nodes(nodes)
        for node in iter_nodes(result):
            node.pop("linked_folder_source", None)
            ref = node.get("media_ref")
            if isinstance(ref, dict):
                ref["file_path"] = to_manifest_url(str(ref.get("file_path") or ""), folder)
            if node.get("resolved_url"):
                node["resolved_url"] = to_manifest_url(str(node.get("resolved_url") or ""), folder)
            thumb = str(node.get("thumbnail_local_path") or "")
            if thumb and self._is_inside(Path(thumb), folder):
                node["thumbnail_local_path"] = to_manifest_url(thumb, folder)
            else:
                node.pop("thumbnail_local_path", None)
                node.pop("thumbnail_cache_key", None)
        return clean_dict(result)

    def _resolve_nodes(self, nodes: list[Node], folder: Path) -> list[Node]:
        result = clone_nodes(nodes)
        for node in iter_nodes(result):
            ref = node.get("media_ref")
            if isinstance(ref, dict):
                ref["file_path"] = from_manifest_url(str(ref.get("file_path") or ""), folder)
            if node.get("resolved_url"):
                node["resolved_url"] = from_manifest_url(str(node.get("resolved_url") or ""), folder)
            if node.get("thumbnail_local_path"):
                node["thumbnail_local_path"] = from_manifest_url(
                    str(node.get("thumbnail_local_path") or ""),
                    folder,
                )
            if node.get("type") == "media":
                node["linked_folder_source"] = str(folder)
        return result

    def _portable_linked_files(
        self,
        linked_folder_files: dict[str, str],
        folder: Path,
    ) -> dict[str, str]:
        return {
            to_manifest_url(str(path), folder): str(node_id)
            for path, node_id in linked_folder_files.items()
            if path and node_id
        }

    def _resolve_linked_files(self, value: Any, folder: Path) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            from_manifest_url(str(path), folder): str(node_id)
            for path, node_id in value.items()
            if path and node_id
        }

    def _portable_imports(
        self,
        imports: dict[str, dict[str, Any]],
        folder: Path,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, record in imports.items():
            if not isinstance(record, dict):
                continue
            portable = clean_dict(record)
            portable_key = str(key)
            if portable.get("path"):
                portable_path = to_manifest_url(str(portable.get("path") or ""), folder)
                portable["path"] = portable_path
                portable["source_key"] = portable_path
                portable_key = portable_path
            result[portable_key] = portable
        return result

    def _resolve_imports(self, value: Any, folder: Path) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, record in value.items():
            if not isinstance(record, dict):
                continue
            resolved = clean_dict(record)
            if resolved.get("path"):
                resolved["path"] = from_manifest_url(str(resolved.get("path") or ""), folder)
            result[str(key)] = resolved
        return result

    def _date_from_block_or_folder(
        self,
        block: dict[str, Any],
        folder: Path,
        identity: MeetingSyncIdentity,
    ) -> date:
        try:
            return date.fromisoformat(str(block.get("folder_date") or ""))
        except ValueError:
            return self._date_from_folder(folder, identity)

    def _date_from_folder(self, folder: Path, identity: MeetingSyncIdentity) -> date:
        try:
            return date.fromisoformat(folder.name.split(maxsplit=1)[0])
        except (IndexError, ValueError):
            return self.folder_date_for(identity.monday, identity.pub_type)

    @staticmethod
    def _revision(block: Any) -> int:
        if not isinstance(block, dict):
            return 0
        try:
            return max(0, int(block.get("revision") or 0))
        except (TypeError, ValueError):
            return 0

    def _merge_conflicting_nodes(
        self,
        existing_nodes: list[Node],
        incoming_nodes: list[Node],
    ) -> list[Node]:
        existing_by_id = {
            self._node_identity(node): node
            for node in existing_nodes
            if self._node_identity(node)
        }

        def merge_level(existing_level: list[Node], incoming_level: list[Node]) -> list[Node]:
            result = clone_nodes(incoming_level)
            seen: set[str] = set()
            for node in result:
                ident = self._node_identity(node)
                if ident:
                    seen.add(ident)
                existing = existing_by_id.get(ident)
                if existing and node.get("type") in {"section", "subsection"}:
                    node["children"] = merge_level(
                        existing.get("children", []),
                        node.get("children", []),
                    )
            for node in existing_level:
                ident = self._node_identity(node)
                if ident and ident not in seen:
                    result.append(clone_nodes([node])[0])
                    seen.add(ident)
            return result

        return merge_level(existing_nodes, incoming_nodes)

    @staticmethod
    def _node_identity(node: Node) -> str:
        return str(node.get("id") or node.get("meeting_source_key") or "")

    def _local_url_fields(self, node: Node):
        ref = node.setdefault("media_ref", {})
        if isinstance(ref, dict):
            yield ref, "file_path"
        yield node, "resolved_url"
        yield node, "thumbnail_local_path"

    def _target_dir_for_node_file(
        self,
        node: Node,
        field: str,
        source_path: Path,
        folder: Path,
        generated_roots: Iterable[Path],
    ) -> Path:
        sync_cache = folder / CACHE_DIR_NAME
        if (
            field == "thumbnail_local_path"
            or node.get("meeting_generated")
            or self._is_inside(source_path, sync_cache)
            or self._is_under_any(source_path, generated_roots)
        ):
            return cache_dir(folder)
        return folder

    def _is_in_target_location(self, source_path: Path, folder: Path, target_dir: Path) -> bool:
        if target_dir == folder:
            try:
                return source_path.resolve().parent == folder.resolve()
            except OSError:
                return False
        return self._is_inside(source_path, target_dir)

    def _primary_media_file_is_direct_child(self, node: Node, folder: Path) -> bool:
        ref = node.get("media_ref")
        if not isinstance(ref, dict):
            return False
        value = str(ref.get("file_path") or "")
        if not value or value.startswith(("http://", "https://")):
            return False
        try:
            return Path(value).resolve().parent == folder.resolve()
        except OSError:
            return False

    @staticmethod
    def _copyable_local_url(value: str) -> bool:
        if not value or value.startswith(("http://", "https://")):
            return False
        path = Path(value)
        return path.is_absolute() and path.is_file()

    @staticmethod
    def _is_inside(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _is_under_any(self, path: Path, roots: Iterable[Path]) -> bool:
        return any(self._is_inside(path, root) for root in roots)

    def _copy_into_directory(self, source: Path, dest_dir: Path) -> Path:
        if not source.is_file():
            raise MeetingSyncError(f"File is not available: {source}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        destination = self._unique_destination(source, dest_dir)
        if self._same_file(source, destination):
            return destination
        temp = dest_dir / f".{destination.name}.{os.getpid()}.tmp"
        try:
            shutil.copy2(source, temp)
            os.replace(temp, destination)
            return destination
        except OSError as exc:
            raise MeetingSyncError(f"Could not copy '{source.name}' into linked folder.") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove temporary copied file %s", temp, exc_info=True)

    def _unique_destination(self, source: Path, dest_dir: Path) -> Path:
        candidate = dest_dir / source.name
        if not candidate.exists() or self._same_file(source, candidate):
            return candidate

        digest = self._file_digest(source)[:10]
        stem = source.stem[:80]
        suffix = source.suffix
        candidate = dest_dir / f"{stem}-{digest}{suffix}"
        if not candidate.exists() or self._same_file(source, candidate):
            return candidate

        counter = 2
        while True:
            numbered = dest_dir / f"{stem}-{digest}-{counter}{suffix}"
            if not numbered.exists() or self._same_file(source, numbered):
                return numbered
            counter += 1

    def _same_file(self, left: Path, right: Path) -> bool:
        try:
            if left.resolve() == right.resolve():
                return True
            if not right.exists():
                return False
            if left.stat().st_size != right.stat().st_size:
                return False
            return self._file_digest(left) == self._file_digest(right)
        except OSError:
            return False

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
