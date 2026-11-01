"""Explicit meeting-folder removal with local, non-destructive crash recovery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.ingest.sync.journal import JournalError
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.media.operations import MediaOperationCancelled
from solin.core.storage.binary_files import write_bytes_atomic

from .folder_matcher import match_meeting_folder
from .linked_folder_sync import MeetingLinkedFolderSync, MeetingSyncError, MeetingSyncIdentity
from .tree_types import Node, clone_nodes, iter_nodes, iter_nodes_strict


class MeetingFolderDeletionError(MeetingSyncError):
    """Deletion cannot safely begin, or its durable local intent is invalid."""


@dataclass(frozen=True, slots=True)
class MeetingFolderDeletionResult:
    nodes: list[Node]
    imports: dict[str, dict[str, Any]]
    removed_node_ids: frozenset[str]
    folder_exists: bool | None
    error: str
    pending: bool

    @property
    def folder_removed(self) -> bool:
        return self.folder_exists is False


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


class MeetingFolderDeletion:
    """Own one meeting's deletion intent until its local snapshot is persisted.

    The caller drains workers and prevents new folder operations. Recovery only
    prunes captured occurrences with proven missing sources; it never deletes
    files. Call ``acknowledge`` only after saving the reconciled local snapshot.
    Serialize deletion and snapshot acknowledgement for each meeting.
    """

    def __init__(
        self,
        root: Path,
        folder: Path,
        identity: MeetingSyncIdentity,
        state_dir: Path,
    ) -> None:
        self.root = _absolute(root)
        self.folder = _absolute(folder)
        self.identity = identity
        self.state_dir = _absolute(state_dir)
        self._intent_path = self._path_for(self.state_dir, self.root, identity)

    @staticmethod
    def _subject(identity: MeetingSyncIdentity) -> dict[str, str]:
        return {
            "tree_key": identity.tree_key,
            "pub_type": identity.pub_type,
            "monday": identity.monday.isoformat(),
            "meeting_tag": identity.meeting_tag,
        }

    @classmethod
    def _path_for(cls, state_dir: Path, root: Path, identity: MeetingSyncIdentity) -> Path:
        key = json.dumps([os.path.normcase(str(root)), cls._subject(identity)], sort_keys=True)
        return state_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    @classmethod
    def pending_for(
        cls,
        state_dir: Path,
        root: Path,
        identity: MeetingSyncIdentity,
    ) -> MeetingFolderDeletion | None:
        """Find local recovery without discovering or accessing the shared folder."""
        path = cls._path_for(_absolute(state_dir), _absolute(root), identity)
        value = cls._read_json(path)
        if value is None:
            return None
        folder = value.get("folder")
        if not isinstance(folder, str) or not Path(folder).is_absolute():
            raise MeetingFolderDeletionError("Invalid meeting-folder deletion target")
        instance = cls(root, Path(folder), identity, state_dir)
        instance._read_intent()
        return instance

    @property
    def pending(self) -> bool:
        return self._read_intent() is not None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise MeetingFolderDeletionError(
                f"Cannot read meeting-folder deletion intent: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise MeetingFolderDeletionError("Invalid meeting-folder deletion intent")
        return value

    def _read_intent(self) -> dict[str, Any] | None:
        value = self._read_json(self._intent_path)
        if value is None:
            return None
        if (
            value.get("version") != 1
            or value.get("root") != str(self.root)
            or value.get("folder") != str(self.folder)
            or value.get("identity") != self._subject(self.identity)
            or not isinstance(value.get("resolved_root"), str)
            or not isinstance(value.get("sources"), dict)
        ):
            raise MeetingFolderDeletionError("Meeting-folder deletion intent identity mismatch")
        for source, association in value["sources"].items():
            if (
                not isinstance(source, str)
                or not Path(source).is_absolute()
                or _absolute(source) != Path(source)
                or not Path(source).is_relative_to(self.folder)
                or not isinstance(association, dict)
                or any(
                    not isinstance(association.get(field), list)
                    or any(not isinstance(item, str) for item in association[field])
                    for field in ("node_ids", "import_keys")
                )
            ):
                raise MeetingFolderDeletionError("Invalid meeting-folder deletion sources")
        return value

    def _check_root(self) -> Path:
        if self.root.is_symlink() or self.root.is_junction():
            raise MeetingFolderDeletionError("Linked root must not be a symbolic link or junction")
        if not stat.S_ISDIR(self.root.stat().st_mode):
            raise MeetingFolderDeletionError("Linked root is not a directory")
        # stat alone does not establish that a mounted or remote root is readable.
        with os.scandir(self.root) as entries:
            next(entries, None)
        return self.root.resolve(strict=True)

    def _validate_location(self, intent: dict[str, Any] | None = None) -> bool:
        resolved_root = self._check_root()
        if intent is not None and str(resolved_root) != intent["resolved_root"]:
            raise MeetingFolderDeletionError("Linked root changed after deletion was requested")
        if self.folder == self.root or not self.folder.is_relative_to(self.root):
            raise MeetingFolderDeletionError(
                "Meeting folder must be a strict child of the linked root"
            )
        match = match_meeting_folder(self.folder.name)
        if (
            match is None
            or match.monday != self.identity.monday
            or match.meeting_tag != self.identity.meeting_tag
        ):
            raise MeetingFolderDeletionError("Folder name does not match this meeting")
        if self.state_dir.resolve().is_relative_to(resolved_root):
            raise MeetingFolderDeletionError(
                "Deletion intents must be stored outside the linked root"
            )
        current = self.folder
        while current != self.root:
            if current.is_symlink() or current.is_junction():
                raise MeetingFolderDeletionError(
                    "Meeting folder must not traverse symbolic links or junctions"
                )
            current = current.parent
        resolved_folder = self.folder.resolve()
        if resolved_folder == resolved_root or not resolved_folder.is_relative_to(resolved_root):
            raise MeetingFolderDeletionError("Meeting folder escapes the linked root")
        try:
            mode = self.folder.lstat().st_mode
        except FileNotFoundError:
            self._check_root()
            return False
        if not stat.S_ISDIR(mode):
            raise MeetingFolderDeletionError("Meeting folder is not a directory")
        # An unreadable folder cannot establish missing individual sources.
        with os.scandir(self.folder) as entries:
            next(entries, None)
        return True

    def _local_source(self, value: object) -> str | None:
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None
        path = _absolute(value)
        return str(path) if path.is_relative_to(self.folder) else None

    def _capture_sources(
        self,
        nodes: list[Node],
        imports: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, list[str]]]:
        all_nodes = list(iter_nodes_strict(nodes))
        by_id = {str(node.get("id") or ""): node for node in all_nodes}
        sources: dict[str, dict[str, set[str]]] = {}

        def associate(path: str, node_ids=(), import_keys=()):
            entry = sources.setdefault(path, {"node_ids": set(), "import_keys": set()})
            entry["node_ids"].update(node_ids)
            entry["import_keys"].update(import_keys)

        imported_ids: set[str] = set()
        for key, record in imports.items():
            source = self._local_source(record.get("path"))
            if source is None:
                continue
            associated: set[str] = set()
            for node_id in record.get("node_ids", []):
                node = by_id.get(str(node_id))
                if node is not None:
                    associated.update(
                        str(child["id"])
                        for child in iter_nodes([node])
                        if child.get("id") and not child.get("meeting_generated")
                    )
            imported_ids.update(associated)
            associate(source, associated, [key])
        for node in all_nodes:
            node_id = str(node.get("id") or "")
            if not node_id or node.get("meeting_generated") or node_id in imported_ids:
                continue
            ref = node.get("media_ref") or {}
            source = self._local_source(ref.get("file_path")) or self._local_source(
                node.get("resolved_url")
            )
            if source is not None and node.get("type") == "media":
                associate(source, [node_id])
            elif self._local_source(node.get("linked_folder_source")) == str(self.folder):
                # A detached converted output without an import record has no
                # provable individual source; remove it only with the whole folder.
                associate(str(self.folder), [node_id])
        return {
            path: {field: sorted(values) for field, values in entry.items()}
            for path, entry in sources.items()
        }

    def delete(
        self,
        file_store: WatchedFolderFileStore,
        sync_service: MeetingLinkedFolderSync,
        *,
        nodes: list[Node],
        imports: dict[str, dict[str, Any]],
        cancellation: CancellationFlag,
    ) -> MeetingFolderDeletionResult:
        """Remove only on explicit invocation; retain intent until snapshot acknowledgement."""
        if cancellation.is_set():
            raise MediaOperationCancelled("Meeting folder deletion cancelled")
        destructive_started = False
        try:
            existing = self._read_intent()
            self._validate_location(existing)
            sources = self._capture_sources(nodes, imports)
            if existing is not None:
                for source, association in existing["sources"].items():
                    entry = sources.setdefault(source, {"node_ids": [], "import_keys": []})
                    for field in entry:
                        entry[field] = sorted(set(entry[field]) | set(association[field]))
            with sync_service.retiring_inactive_folder(
                self.folder, self.identity
            ) as require_inactive:
                folder_exists = self._validate_location(existing)
                intent = {
                    "version": 1,
                    "root": str(self.root),
                    "folder": str(self.folder),
                    "resolved_root": str(self.root.resolve(strict=True)),
                    "identity": self._subject(self.identity),
                    "sources": sources,
                }
                write_bytes_atomic(
                    self._intent_path, json.dumps(intent, sort_keys=True).encode("utf-8")
                )
                if cancellation.is_set():
                    raise MediaOperationCancelled("Meeting folder deletion cancelled")
                if folder_exists:
                    self._validate_location(intent)
                    require_inactive()
                    destructive_started = True
                    file_store.delete_folder(self.folder)
        except (MeetingSyncError, JournalError, OSError, ValueError) as exc:
            if destructive_started:
                return replace(self.reconcile(nodes, imports), error=str(exc) or type(exc).__name__)
            raise MeetingFolderDeletionError(str(exc) or type(exc).__name__) from exc
        return self.reconcile(nodes, imports)

    def reconcile(
        self,
        nodes: list[Node],
        imports: dict[str, dict[str, Any]],
    ) -> MeetingFolderDeletionResult:
        """Reconcile a prior explicit deletion without performing filesystem mutations."""
        intent = self._read_intent()
        result = MeetingFolderDeletionResult(
            clone_nodes(nodes),
            deepcopy(imports),
            frozenset(),
            None,
            "",
            intent is not None,
        )
        if intent is None:
            return result
        try:
            folder_exists = self._validate_location(intent)
            missing: set[str] = set()
            for source in intent["sources"]:
                path = Path(source)
                if not folder_exists:
                    missing.add(source)
                else:
                    ancestor = path.parent
                    while ancestor.is_relative_to(self.folder):
                        if ancestor.is_symlink() or ancestor.is_junction():
                            raise MeetingFolderDeletionError(
                                "Cannot verify a source through a symbolic link or junction"
                            )
                        ancestor = ancestor.parent
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        missing.add(source)
            # Do not interpret root disappearance during probing as source absence.
            if self._validate_location(intent) != folder_exists:
                raise MeetingFolderDeletionError("Meeting folder changed during deletion recovery")
        except (OSError, ValueError, MeetingSyncError) as exc:
            return replace(result, error=str(exc) or type(exc).__name__)
        removed_ids = {
            node_id for source in missing for node_id in intent["sources"][source]["node_ids"]
        }
        surviving_ids = {
            node_id
            for source, association in intent["sources"].items()
            if source not in missing
            for node_id in association["node_ids"]
        }
        removed_ids.difference_update(surviving_ids)
        removed_imports = {
            key for source in missing for key in intent["sources"][source]["import_keys"]
        }
        actually_removed: set[str] = set()

        def prune(children: list[Node]) -> list[Node]:
            retained = []
            for node in children:
                if "children" in node:
                    node["children"] = prune(node["children"])
                node_id = str(node.get("id") or "")
                if node_id in removed_ids and not node.get("meeting_generated"):
                    # Keep a container if canonical or unrelated children remain.
                    if not node.get("children"):
                        actually_removed.add(node_id)
                        continue
                retained.append(node)
            return retained

        reconciled = prune(result.nodes)
        return replace(
            result,
            nodes=reconciled,
            imports={
                key: value for key, value in result.imports.items() if key not in removed_imports
            },
            removed_node_ids=frozenset(actually_removed),
            folder_exists=folder_exists,
            error="Meeting folder deletion is incomplete" if folder_exists else "",
        )

    def acknowledge(self) -> None:
        """Clear recovery only after the caller durably saved the reconciled snapshot."""
        if self._read_intent() is None:
            return
        self._intent_path.unlink()
        if os.name != "nt":
            descriptor = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
