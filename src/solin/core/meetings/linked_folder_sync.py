"""Shared linked-folder sync for meeting trees."""
from __future__ import annotations

import hashlib
import logging
import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.media.operations import MediaOperationCancelled
from solin.core.storage.binary_files import write_bytes_atomic

from solin.core.ingest.manifest import (
    CACHE_DIR_NAME,
    MANIFEST_FILE,
    MANIFEST_REPOSITORY,
    ManifestError,
    cache_dir,
    from_manifest_url,
    to_manifest_url,
)

from solin.core.ingest.sync.journal import (
    JournalError,
    JournalReplica,
    JournalUnavailable,
    ReplicaSnapshot,
)
from solin.core.ingest.sync.activation import (
    ActivationError,
    ActivationStore,
    DocumentActivation,
)
from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes
from solin.core.ingest.sync.discovery import (
    AUXILIARY, CONTENT, DISCOVERED, RESOURCE, SUPPRESSED, SUPPRESSED_CONTENTS,
    ResourceSuppressions, reconcile_discoveries, record_discovery_edits,
)
from solin.core.ingest.sync.resources import (
    archived_content_signatures, content_identity, content_signature, portable_resource_key,
    recover_file, retire_file, resource_identity_key,
)
from solin.core.ingest.watched_folder_files import (
    WatchedFolderCopyRequest, WatchedFolderCopyResult, WatchedFolderFileStore,
)

from .folder_matcher import match_meeting_folder
from .tree_types import Node, clean_dict, clone_nodes, iter_nodes

log = logging.getLogger(__name__)

MEETING_TREE_KEY = "meeting_tree"
MEETING_TREE_SCHEMA_VERSION = 4
_MEETING_META = "$meeting"
MeetingWeekdayResolver = Callable[[str], int]


class MeetingSyncError(RuntimeError):
    """Raised when meeting linked-folder sync cannot complete safely."""


class MeetingSyncInactive(MeetingSyncError):
    """A save belongs to a meeting sync that was explicitly deactivated."""


class MeetingSyncPending(MeetingSyncError):
    """Activation exists but its journal is not completely available yet."""


class MeetingSyncCleanupPending(MeetingSyncError):
    """Synchronization is inactive but internal files still need cleanup."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


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
    canonical_reset_generation: int = 0
    hidden_canonical_media: dict[str, Node] = dataclass_field(default_factory=dict)
    snapshot: ReplicaSnapshot = dataclass_field(default_factory=ReplicaSnapshot)
    pending_count: int = 0
    waiting_count: int = 0
    resource_error: str = ""


@dataclass(frozen=True, slots=True)
class MeetingSyncDeactivation:
    folder: Path
    cleanup_errors: tuple[str, ...] = ()


def meeting_tag_for_pub_type(pub_type: str) -> str:
    if pub_type == "mwb":
        return "MW"
    if pub_type == "wt":
        return "WE"
    raise MeetingSyncError(f"Meeting sync is not supported for '{pub_type}'.")


class MeetingLinkedFolderSync:
    """Domain service for meeting state stored in a watched meeting folder."""

    def __init__(self, weekday_for_pub_type: MeetingWeekdayResolver) -> None:
        self._weekday_for_pub_type = weekday_for_pub_type
        self._replicas: dict[str, JournalReplica] = {}
        self._verified_legacy: set[Path] = set()
        self._activation_lock = threading.RLock()
        self._activating: dict[str, int] = {}

    def folder_date_for(self, monday: date, pub_type: str) -> date:
        weekday = self._weekday_for_pub_type(pub_type)
        if not 0 <= weekday <= 6:
            return monday
        return monday + timedelta(days=weekday)

    def folder_name_for(self, identity: MeetingSyncIdentity) -> str:
        folder_date = self.folder_date_for(identity.monday, identity.pub_type)
        return f"{folder_date.isoformat()} {identity.meeting_tag}"

    @staticmethod
    def activation_path(folder: Path) -> Path:
        return ActivationStore(folder, "meeting").path

    @staticmethod
    def _folder_key(folder: Path) -> str:
        return os.path.normcase(os.path.abspath(folder))

    @staticmethod
    def _activation_subject(identity: MeetingSyncIdentity) -> dict[str, str]:
        return {
            "tree_key": identity.tree_key,
            "pub_type": identity.pub_type,
            "monday": identity.monday.isoformat(),
            "meeting_tag": identity.meeting_tag,
        }

    def _activation(
        self, folder: Path, identity: MeetingSyncIdentity,
    ) -> DocumentActivation | None:
        try:
            activation = ActivationStore(folder, "meeting").read()
        except (ActivationError, OSError) as exc:
            raise MeetingSyncPending(str(exc)) from exc
        if activation is None:
            return None
        if activation.subject != self._activation_subject(identity):
            raise MeetingSyncError("Activation marker belongs to another meeting")
        return activation

    @contextmanager
    def _activation_publication(self, folder: Path):
        key = self._folder_key(folder)
        owner = threading.get_ident()
        with self._activation_lock:
            existing = self._activating.get(key)
            if existing is not None and existing != owner:
                raise MeetingSyncError("Meeting sync activation is already in progress")
            self._activating[key] = owner
        try:
            yield
        finally:
            with self._activation_lock:
                if self._activating.get(key) == owner:
                    self._activating.pop(key, None)

    def _require_active_publication(self, folder: Path, document_id: str) -> None:
        key = self._folder_key(folder)
        with self._activation_lock:
            if self._activating.get(key) == threading.get_ident():
                return
        try:
            activation = ActivationStore(folder, "meeting").read()
        except (ActivationError, OSError) as exc:
            raise MeetingSyncPending(str(exc)) from exc
        if activation is None or activation.document_id != document_id:
            raise MeetingSyncInactive("Meeting sync is not active for this document")

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
        marker = self.activation_path(folder)
        if marker.exists():
            try:
                return self._activation(folder, identity) is not None
            except MeetingSyncError:
                return True  # The marker remains authoritative while incomplete.
        try:
            block = MANIFEST_REPOSITORY.load(folder, strict=True).get(MEETING_TREE_KEY)
        except ManifestError:
            return False
        return self._block_matches(block, identity)

    def _replica(self, folder: Path) -> JournalReplica:
        key = self._folder_key(folder)
        if key not in self._replicas:
            self._replicas[key] = JournalReplica(
                folder,
                "meeting",
                publication_guard=lambda document_id, target=folder: (
                    self._require_active_publication(target, document_id)
                ),
            )
        return self._replicas[key]

    def load_tree(
        self,
        watched_root: str,
        identity: MeetingSyncIdentity,
        *,
        cleanup_inactive: bool = True,
    ) -> MeetingSyncRecord | None:
        folder = self.locate_folder(watched_root, identity, create=False)
        if folder is None:
            return None
        replica = self._replica(folder)
        activation = self._activation(folder, identity)
        if activation is None:
            if replica.document_id is not None or replica.has_retired_documents:
                return self._finish_inactive_load(folder, cleanup=cleanup_inactive)
            return self._migrate_legacy_tree(
                folder,
                identity,
                replica,
                cleanup_inactive=cleanup_inactive,
            )
        if replica.is_retired(activation.document_id):
            try:
                ActivationStore(folder, "meeting").remove(activation.document_id)
            except (ActivationError, OSError):
                pass
            return self._finish_inactive_load(folder, cleanup=cleanup_inactive)
        try:
            replica.require_document(activation.document_id)
            legacy_seed = self._active_legacy_seed(folder, identity)
            snapshot = (
                replica.read_verified_seed(legacy_seed)
                if legacy_seed is not None
                else replica.read()
            )
        except JournalUnavailable as exc:
            raise MeetingSyncPending(str(exc)) from exc
        except JournalError as exc:
            raise MeetingSyncError(str(exc)) from exc
        metadata = snapshot.entities.get(_MEETING_META, {})
        if not metadata:
            raise MeetingSyncPending("Activated meeting journal has not arrived yet")
        snapshot = self._bind_resource_contents(folder, snapshot)
        record = self._record_from_snapshot(folder, identity, snapshot)
        record = replace(record, resource_error=self._reconcile_resource_files(folder, snapshot))
        self._remove_legacy_manifest(folder)
        return record

    def _finish_inactive_load(
        self,
        folder: Path,
        *,
        cleanup: bool,
    ) -> None:
        if not cleanup:
            return None
        result = self.cleanup_inactive_tree(folder)
        if result.cleanup_errors:
            raise MeetingSyncCleanupPending(result.cleanup_errors)
        return None

    def _active_legacy_seed(
        self,
        folder: Path,
        identity: MeetingSyncIdentity,
    ) -> dict[str, dict[str, Any]] | None:
        """Return a valid local baseline that must agree with a received marker."""

        if folder in self._verified_legacy or not (folder / MANIFEST_FILE).exists():
            return None
        try:
            manifest = MANIFEST_REPOSITORY.load(folder, strict=True)
        except ManifestError:
            return None  # A partial legacy file cannot override a valid activation marker.
        block = manifest.get(MEETING_TREE_KEY)
        if not isinstance(block, dict) or not self._block_matches(block, identity):
            return None
        seed = self._entities_from_block(block)
        for entity in seed.values():
            ref = entity.get("media_ref", {})
            url = str(ref.get("file_path") or entity.get("resolved_url") or "")
            if entity.get("type") == "media" and url:
                entity[RESOURCE] = portable_resource_key(url, folder)
        return seed

    def _migrate_legacy_tree(
        self,
        folder: Path,
        identity: MeetingSyncIdentity,
        replica: JournalReplica,
        *,
        cleanup_inactive: bool,
    ) -> MeetingSyncRecord | None:
        try:
            manifest, original = MANIFEST_REPOSITORY.load_frozen(folder, strict=True)
        except ManifestError as exc:
            raise MeetingSyncError(str(exc)) from exc
        block = manifest.get(MEETING_TREE_KEY)
        if not isinstance(block, dict) or not self._block_matches(block, identity):
            if replica.document_id is not None:
                replica.retire_binding()
            return self._finish_inactive_load(folder, cleanup=cleanup_inactive)
        version = self._int_value(block.get("schema_version"))
        if version > MEETING_TREE_SCHEMA_VERSION:
            raise MeetingSyncError("Unsupported meeting sync version; update every terminal.")
        if original is None:
            raise MeetingSyncError("Legacy meeting baseline has no original bytes.")
        backup = (
            folder / ".solin_sync" / "meeting" / "migration"
            / f"{hashlib.sha256(original).hexdigest()}.json"
        )
        if not backup.exists():
            write_bytes_atomic(backup, original)
        seed = self._entities_from_block(block)
        for entity in seed.values():
            ref = entity.get("media_ref", {})
            url = str(ref.get("file_path") or entity.get("resolved_url") or "")
            if entity.get("type") == "media" and url:
                entity[RESOURCE] = portable_resource_key(url, folder)
        try:
            with self._activation_publication(folder):
                snapshot = replica.read(seed=seed)
                document_id = snapshot.document_id
                if document_id is None:
                    raise MeetingSyncError("Legacy migration did not establish a document")
                ActivationStore(folder, "meeting").publish(
                    document_id, self._activation_subject(identity)
                )
                replica.require_document(document_id)
        except (ActivationError, JournalError, OSError) as exc:
            raise MeetingSyncError(str(exc)) from exc
        self._verified_legacy.add(folder)
        record = self._record_from_snapshot(folder, identity, snapshot)
        record = replace(record, resource_error=self._reconcile_resource_files(folder, snapshot))
        try:
            self._write_materialization(record, identity)
        except (OSError, ValueError):
            log.warning("Could not cache migrated meeting materialization", exc_info=True)
        self._remove_legacy_manifest(folder)
        return record

    @staticmethod
    def _remove_legacy_manifest(folder: Path) -> None:
        try:
            MANIFEST_REPOSITORY.delete(folder)
        except ManifestError:
            log.warning("Could not remove migrated meeting manifest", exc_info=True)

    def resume_local_tree(
        self, folder: Path, identity: MeetingSyncIdentity, known: ReplicaSnapshot,
    ) -> MeetingSyncRecord:
        """Recover accepted local operations while the linked transport is offline."""
        replica = self._replica(folder)
        snapshot = replica.read_local()
        if not snapshot.operation_ids and known.operation_ids:
            snapshot = replica.stage(known, known.entities)
        return self._record_from_snapshot(folder, identity, snapshot)

    def _reconcile_resource_files(self, folder: Path, snapshot: ReplicaSnapshot) -> str:
        projected = reconcile_discoveries(snapshot.entities)
        active = {
            resource_identity_key(str(node[RESOURCE])): str(node[RESOURCE])
            for node in projected.values() if node.get(RESOURCE) and not node.get(AUXILIARY)
        }
        suppressed = {
            resource_identity_key(str(node[SUPPRESSED])): str(node[SUPPRESSED])
            for node in projected.values() if node.get(SUPPRESSED)
        }
        suppressions = ResourceSuppressions(snapshot.entities)
        error = ""
        # Suppression records are the durable cleanup queue. Retrying after a
        # restart never requires guessing from a missing filesystem entry.
        for identity, resource in (suppressed | active).items():
            if resource.startswith(("http://", "https://")):
                continue
            try:
                if identity in active:
                    recover_file(folder, resource, document_id=snapshot.document_id)
                else:
                    versions = suppressions.versions(resource)
                    if versions is None:
                        # Legacy deletions must not claim today's replacement bytes.
                        raise OSError("Deleted resource content is not yet known")
                    retire_file(
                        folder, resource, document_id=snapshot.document_id,
                        expected_contents=versions,
                    )
            except (OSError, ValueError) as exc:
                error = str(exc)
                log.warning("Meeting resource reconciliation pending: %s", resource, exc_info=True)
        return error

    def _bind_resource_contents(
        self, folder: Path, snapshot: ReplicaSnapshot,
    ) -> ReplicaSnapshot:
        """Persist observed active content and verified legacy deletion versions."""
        updates: dict[str, dict] = {}
        unbound = {
            key for key, node in snapshot.entities.items()
            if node.get(RESOURCE) and not node.get(AUXILIARY) and CONTENT not in node
        }
        projected = reconcile_discoveries(snapshot.entities) if unbound else {}
        for key in unbound & projected.keys():
            node = projected[key]
            resource = str(node.get(RESOURCE) or "")
            if (
                not resource or node.get(AUXILIARY) or content_identity(node.get(CONTENT)) is not None
                or resource.startswith(("http://", "https://"))
            ):
                continue
            try:
                path = Path(from_manifest_url(portable_resource_key(resource, folder), folder))
                if path.is_file():
                    updates[key] = {**snapshot.entities[key], CONTENT: content_signature(path)}
            except (OSError, ValueError):
                continue  # Missing media never changes the visible occurrence.
        archives: dict[str, list[dict[str, str | int]]] = {}
        legacy = {
            key for key, node in snapshot.entities.items()
            if node.get(SUPPRESSED) and SUPPRESSED_CONTENTS not in node
        }
        contexts = self._replica(folder).entity_creation_contexts(snapshot, legacy) if legacy else {}
        for key, node in snapshot.entities.items():
            resource = str(node.get(SUPPRESSED) or "")
            if not resource or SUPPRESSED_CONTENTS in node or resource.startswith(("http://", "https://")):
                continue
            identity = resource_identity_key(resource)
            try:
                if identity not in archives:
                    archives[identity] = archived_content_signatures(
                        folder, resource, document_id=snapshot.document_id,
                    )
            except (OSError, ValueError):
                continue  # Cleanup reports errors and retries when transport catches up.
            signatures: set[tuple[int, str]] = set()
            creation_views = contexts.get(key, [])
            for before in creation_views:
                occurrence_id = str(node.get("occurrence_id") or "")
                occurrence = before.entities.get(occurrence_id, {})
                signature = content_identity(occurrence.get(CONTENT))
                if signature is None:
                    records = before.entities.get(_MEETING_META, {})
                    candidates = {
                        content_identity(record.get("signature"))
                        for field, record in records.items()
                        if field.startswith("meeting_folder_imports:") and isinstance(record, dict)
                        and record.get("kind") == "media" and occurrence_id in record.get("node_ids", [])
                        and resource_identity_key(str(record.get("path") or "")) == identity
                    }
                    if len(candidates) == 1:
                        signature = next(iter(candidates))
                if signature is None:
                    break
                signatures.add(signature)
            else:
                if len(signatures) == 1 and len(archives[identity]) == 1:
                    signature = next(iter(signatures))
                    if content_identity(archives[identity][0]) == signature:
                        updates[key] = {**node, SUPPRESSED_CONTENTS: archives[identity]}
        if not updates:
            return snapshot
        try:
            return self._replica(folder).commit(snapshot, {**snapshot.entities, **updates})
        except JournalError as exc:
            raise MeetingSyncError(str(exc)) from exc

    def _entities_from_block(
        self, block: dict[str, Any], baseline: ReplicaSnapshot | None = None,
    ) -> dict[str, dict[str, Any]]:
        entities = flatten_nodes(block.get("nodes", []), (baseline or ReplicaSnapshot()).entities)
        metadata: dict[str, Any] = {AUXILIARY: True}
        for key in ("tree_key", "pub_type", "monday", "meeting_tag", "folder_date",
                    "canonical_reset_generation"):
            metadata[key] = block.get(key)
        generation = int(block.get("canonical_reset_generation") or 0)
        canonical = {AUXILIARY: True, "generation": generation}
        for key in block.get("deleted_source_keys", []):
            canonical[f"deleted:{key}"] = True
        for key, value in block.get("hidden_canonical_media", {}).items():
            canonical[f"hidden_canonical_media:{key}"] = value
        entities[f"$canonical:{generation}"] = canonical
        for entity in entities.values():
            if entity.get("meeting_generated"):
                entity["__sync_canonical_generation"] = generation
        for field in ("linked_folder_files", "meeting_folder_imports"):
            for key, value in block.get(field, {}).items():
                metadata[f"{field}:{key}"] = value
        entities[_MEETING_META] = metadata
        return entities

    def _record_from_snapshot(
        self, folder: Path, identity: MeetingSyncIdentity, snapshot: ReplicaSnapshot,
    ) -> MeetingSyncRecord:
        projected = reconcile_discoveries(snapshot.entities)
        metadata = dict(projected.get(_MEETING_META, {}))
        canonical = max(
            (value for key, value in projected.items() if key.startswith("$canonical:")),
            key=lambda value: int(value.get("generation") or 0), default={},
        )
        generation = int(canonical.get("generation") or 0)
        metadata.update(canonical)
        metadata["canonical_reset_generation"] = generation
        projected = {
            key: value for key, value in projected.items()
            if not value.get("meeting_generated") or int(value.get("__sync_canonical_generation") or 0) >= generation
        }
        block = {key: value for key, value in metadata.items() if ":" not in key}
        block["nodes"] = rebuild_nodes({
            key: value for key, value in projected.items() if not value.get(AUXILIARY)
        })
        block["deleted_source_keys"] = [
            key.removeprefix("deleted:") for key, value in metadata.items()
            if key.startswith("deleted:") and value
        ]
        for field in ("linked_folder_files", "meeting_folder_imports", "hidden_canonical_media"):
            block[field] = {
                key.removeprefix(f"{field}:"): value for key, value in metadata.items()
                if key.startswith(f"{field}:")
            }
        block["revision"] = len(snapshot.operation_ids)
        block["canonical_hash"] = identity.canonical_hash
        record = self._record_from_block(folder, block, identity)
        return replace(
            record, snapshot=snapshot,
            pending_count=self._replica(folder).pending_count,
            waiting_count=self._replica(folder).waiting_count,
        )

    def _write_materialization(self, record: MeetingSyncRecord, identity: MeetingSyncIdentity) -> None:
        block = self._block_from_tree(
            record.folder, identity, nodes=record.nodes,
            deleted_source_keys=record.deleted_source_keys,
            linked_folder_files=record.linked_folder_files,
            meeting_folder_imports=record.meeting_folder_imports,
            canonical_reset_generation=record.canonical_reset_generation,
            hidden_canonical_media=record.hidden_canonical_media,
            revision=record.revision,
        )
        block["journal_token"] = record.snapshot.token
        path = self._replica(record.folder).operations_dir.parent / "snapshot.json"
        encoded = json.dumps(block, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if path.exists():
            try:
                existing = json.loads(path.read_bytes())
                if isinstance(existing, dict) and existing.get("journal_token") == record.snapshot.token:
                    return
            except (OSError, ValueError):
                pass  # Advisory snapshots may arrive partially; the journal is authoritative.
        write_bytes_atomic(path, encoded)

    def save_tree(
        self, folder: Path, identity: MeetingSyncIdentity, *, nodes: list[Node],
        deleted_source_keys: set[str], linked_folder_files: dict[str, str],
        meeting_folder_imports: dict[str, dict[str, Any]], expected_revision: int = 0,
        canonical_reset_generation: int = 0,
        hidden_canonical_media: dict[str, Node] | None = None,
        base_snapshot: ReplicaSnapshot | None = None,
        stage_only: bool = False,
        enable: bool = False,
    ) -> MeetingSyncRecord:
        """Persist only edits relative to the caller's actual observed state.

        Numeric revisions are display metadata. Causal snapshots, including
        tombstones archived locally, define edit intent across terminals.
        """
        replica = self._replica(folder)

        def persist() -> MeetingSyncRecord:
            if enable:
                if self._activation(folder, identity) is not None:
                    raise MeetingSyncError("Meeting sync is already active")
                replica.reset_document()
                base = replica.read()
            else:
                if not stage_only:
                    activation = self._activation(folder, identity)
                    if activation is None:
                        raise MeetingSyncInactive("Meeting sync is not active")
                    try:
                        replica.require_document(activation.document_id)
                    except JournalUnavailable as exc:
                        raise MeetingSyncPending(str(exc)) from exc
                    except JournalError as exc:
                        raise MeetingSyncError(str(exc)) from exc
                if base_snapshot is None:
                    try:
                        base = replica.read()
                    except JournalUnavailable as exc:
                        raise MeetingSyncPending(str(exc)) from exc
                    except JournalError as exc:
                        raise MeetingSyncError(str(exc)) from exc
                else:
                    base = base_snapshot
            block = self._block_from_tree(
                folder, identity, nodes=nodes, deleted_source_keys=deleted_source_keys,
                linked_folder_files=linked_folder_files,
                meeting_folder_imports=meeting_folder_imports,
                canonical_reset_generation=canonical_reset_generation,
                hidden_canonical_media=hidden_canonical_media or {}, revision=expected_revision,
            )
            for node in iter_nodes(block["nodes"]):
                ref = node.get("media_ref", {})
                url = str(ref.get("file_path") or node.get("resolved_url") or "")
                if (
                    node.get("type") == "media"
                    and url
                    and not url.startswith(("http://", "https://"))
                ):
                    try:
                        if not stage_only:
                            node[RESOURCE] = portable_resource_key(url, folder)
                        elif not node.get(RESOURCE):
                            node[RESOURCE] = url.replace("\\", "/")
                        previous = base.entities.get(str(node.get("id") or ""), {})
                        if (
                            content_identity(node.get(CONTENT)) is None
                            and content_identity(previous.get(CONTENT)) is not None
                            and resource_identity_key(str(previous.get(RESOURCE) or ""))
                            == resource_identity_key(str(node.get(RESOURCE) or ""))
                        ):
                            node[CONTENT] = dict(previous[CONTENT])
                        if not stage_only and content_identity(node.get(CONTENT)) is None:
                            path = Path(from_manifest_url(node[RESOURCE], folder))
                            if path.is_file():
                                node[CONTENT] = content_signature(path)
                    except ValueError as exc:
                        raise MeetingSyncError(str(exc)) from exc
            desired = record_discovery_edits(base.entities, self._entities_from_block(block, base))
            previous_generation = int(
                base.entities.get(_MEETING_META, {}).get("canonical_reset_generation") or 0
            )
            restores = {
                key for key, value in desired.items() if value.get("meeting_generated")
            } if canonical_reset_generation > previous_generation else set()
            try:
                action = replica.stage if stage_only else replica.commit
                snapshot = action(base, desired, restore_ids=restores)
            except JournalError as exc:
                raise MeetingSyncError(str(exc)) from exc
            if enable:
                document_id = snapshot.document_id
                if document_id is None:
                    raise MeetingSyncError("Meeting activation has no document identity")
                try:
                    ActivationStore(folder, "meeting").publish(
                        document_id, self._activation_subject(identity)
                    )
                    replica.require_document(document_id)
                except (ActivationError, JournalError, OSError) as exc:
                    replica.retire_binding(document_id, include_known=False)
                    raise MeetingSyncError(str(exc)) from exc
            record = self._record_from_snapshot(folder, identity, snapshot)
            if not stage_only:
                self._verified_legacy.add(folder)
                record = replace(
                    record,
                    resource_error=self._reconcile_resource_files(folder, snapshot),
                )
                try:
                    self._write_materialization(record, identity)
                except (OSError, ValueError):
                    log.warning("Could not cache meeting sync materialization", exc_info=True)
            return record

        if enable and not stage_only:
            with self._activation_publication(folder):
                return persist()
        return persist()

    def deactivate_tree(
        self, folder: Path, identity: MeetingSyncIdentity, *, cleanup: bool = True,
    ) -> MeetingSyncDeactivation:
        """Retire activation, optionally deferring cleanup until local state is durable."""

        replica = self._replica(folder)
        activation = self._activation(folder, identity)
        try:
            # A legacy manifest must not be able to remigrate after the
            # activation marker disappears on another replica.
            MANIFEST_REPOSITORY.delete(folder)
        except ManifestError as exc:
            raise MeetingSyncError(str(exc)) from exc
        if activation is not None:
            try:
                with replica.retiring_binding(
                    activation.document_id,
                    include_known=False,
                ):
                    ActivationStore(folder, "meeting").remove(activation.document_id)
            except (ActivationError, JournalError, OSError) as exc:
                raise MeetingSyncError(str(exc)) from exc
        elif replica.document_id is not None:
            replica.retire_binding()

        return self.cleanup_inactive_tree(folder) if cleanup else MeetingSyncDeactivation(folder)

    def cleanup_inactive_tree(self, folder: Path) -> MeetingSyncDeactivation:
        """Idempotently remove all meeting sync metadata after deactivation."""

        errors: list[str] = []
        self._ensure_inactive_cleanup(folder)
        replica = self._replica(folder)
        if replica.document_id is not None:
            try:
                replica.retire_binding()
            except (JournalError, OSError) as exc:
                errors.append(f"local binding: {exc}")
                log.warning("Meeting sync retirement remains pending", exc_info=True)
        try:
            self._ensure_inactive_cleanup(folder)
            MANIFEST_REPOSITORY.delete(folder)
        except ManifestError as exc:
            errors.append(f"{MANIFEST_FILE}: {exc}")
            log.warning("Meeting legacy manifest cleanup remains pending", exc_info=True)
        errors.extend(self._cleanup_inactive_tree(folder))
        self._verified_legacy.discard(folder)
        return MeetingSyncDeactivation(folder, tuple(errors))

    def remove_media_file(
        self,
        folder: Path,
        identity: MeetingSyncIdentity,
        file_path: str,
        *,
        document_id: str | None,
        file_store: WatchedFolderFileStore,
    ) -> bool:
        """Apply a removal in the current mode without touching a replacement document."""
        activation = self._activation(folder, identity)
        if activation is None:
            return file_store.remove_file_inside(file_path, folder)
        if activation.document_id != document_id:
            raise MeetingSyncInactive("Media removal belongs to another meeting sync generation")
        replica = self._replica(folder)
        replica.require_document(activation.document_id)
        snapshot = self._bind_resource_contents(folder, replica.read())
        resource = portable_resource_key(file_path, folder)
        projected = reconcile_discoveries(snapshot.entities)
        if any(
            resource_identity_key(str(node.get(RESOURCE) or "")) == resource_identity_key(resource)
            and not node.get(AUXILIARY) for node in projected.values()
        ):
            return False
        versions = ResourceSuppressions(snapshot.entities).versions(resource)
        if versions is None:
            return False
        return retire_file(
            folder, resource, document_id=document_id,
            expected_contents=versions,
        )

    @staticmethod
    def _ensure_inactive_cleanup(folder: Path) -> None:
        try:
            activation = ActivationStore(folder, "meeting").read()
        except (ActivationError, OSError) as exc:
            raise MeetingSyncPending(str(exc)) from exc
        if activation is not None:
            raise MeetingSyncError("Meeting sync was activated while cleanup was pending")

    def _cleanup_inactive_tree(self, folder: Path) -> tuple[str, ...]:
        errors: list[str] = []
        for path in (folder / CACHE_DIR_NAME, folder / ".solin_sync"):
            if not path.exists():
                continue
            try:
                self._ensure_inactive_cleanup(folder)
                shutil.rmtree(path)
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                log.warning("Meeting sync cleanup remains pending: %s", path, exc_info=True)
        return tuple(errors)

    def materialize_tree_files(
        self,
        nodes: list[Node],
        folder: Path,
        *,
        generated_roots: Iterable[str],
        cancellation: CancellationFlag | None = None,
        created_files_out: list[WatchedFolderCopyResult] | None = None,
    ) -> tuple[list[Node], dict[str, str]]:
        copied: dict[tuple[str, str], Path] = {}
        materialized = clone_nodes(nodes)
        linked_files: dict[str, str] = {}
        roots = [Path(root) for root in generated_roots if root]
        created_files: list[WatchedFolderCopyResult] = []

        try:
            for node in iter_nodes(materialized):
                if cancellation is not None and cancellation.is_set():
                    raise MediaOperationCancelled("Meeting media copy cancelled")
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
                            dest_path = self._copy_into_directory(
                                source_path,
                                dest_dir,
                                cancellation=cancellation,
                                created_files=created_files,
                            )
                            copied[cache_key] = dest_path
                    owner[field] = str(dest_path)
                    if node_id and field != "thumbnail_local_path":
                        linked_files.setdefault(str(dest_path), node_id)
                url = str(node.get("media_ref", {}).get("file_path") or node.get("resolved_url") or "")
                if self._copyable_local_url(url) and Path(url).is_file():
                    signature = content_signature(Path(url))
                    if (
                        node.get(DISCOVERED) and content_identity(node.get(CONTENT)) is not None
                        and content_identity(node[CONTENT]) != content_identity(signature)
                    ):
                        raise MediaOperationCancelled("Meeting source changed during media preparation")
                    node[CONTENT] = signature
                    node[RESOURCE] = portable_resource_key(url, folder)
        except BaseException:  # noqa: BLE001 - materialization transaction rollback
            self.rollback_materialized_files(folder, created_files)
            raise
        if created_files_out is not None:
            created_files_out.extend(created_files)
        return materialized, linked_files

    def detach_cache_references(
        self,
        nodes: list[Node],
        folder: Path,
        durable_dir: Path,
        *,
        cancellation: CancellationFlag | None = None,
    ) -> list[Node]:
        detached = clone_nodes(nodes)
        sync_cache = folder / CACHE_DIR_NAME
        durable_dir.mkdir(parents=True, exist_ok=True)

        for node in iter_nodes(detached):
            if cancellation is not None and cancellation.is_set():
                raise MediaOperationCancelled("Meeting sync detach cancelled")
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
                    owner[field] = str(
                        self._copy_into_directory(
                            path,
                            durable_dir,
                            cancellation=cancellation,
                        )
                    )
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

    def rollback_materialized_files(self, folder: Path, files: Iterable[WatchedFolderCopyResult]) -> None:
        """Retain published bytes recoverably: another terminal may reference them."""
        files = tuple(files)
        if not files:
            return
        try:
            snapshot = self._replica(folder).read()
        except (OSError, JournalError):
            log.warning("Could not inspect references for cancelled meeting copy", exc_info=True)
            return
        active = {
            resource_identity_key(str(entity[RESOURCE]))
            for entity in reconcile_discoveries(snapshot.entities).values()
            if entity.get(RESOURCE) and not entity.get(AUXILIARY)
        }
        for value in reversed(files):
            path = value.destination
            try:
                resource = portable_resource_key(str(path), folder)
                if value.content is not None and resource_identity_key(resource) not in active:
                    retire_file(
                        folder, resource, document_id=snapshot.document_id, expected_contents={value.content},
                    )
            except (OSError, ValueError):
                log.warning("Could not archive cancelled meeting copy %s", path, exc_info=True)

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
        hidden_media = self._resolve_node_mapping(
            block.get("hidden_canonical_media", {}),
            folder,
        )
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
            canonical_reset_generation=max(
                0,
                self._int_value(block.get("canonical_reset_generation")),
            ),
            hidden_canonical_media=hidden_media,
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
        canonical_reset_generation: int,
        hidden_canonical_media: dict[str, Node],
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
            "canonical_reset_generation": max(0, int(canonical_reset_generation)),
            "hidden_canonical_media": self._portable_node_mapping(
                hidden_canonical_media,
                folder,
            ),
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
            if thumb and Path(thumb).is_relative_to(folder):
                node["thumbnail_local_path"] = to_manifest_url(thumb, folder)
            else:
                node.pop("thumbnail_local_path", None)
                node.pop("thumbnail_cache_key", None)
        return clean_dict(result)

    def _portable_node_mapping(
        self,
        nodes: dict[str, Node],
        folder: Path,
    ) -> dict[str, Node]:
        result: dict[str, Node] = {}
        for key, node in nodes.items():
            portable = self._portable_nodes([node], folder)
            if portable:
                result[str(key)] = portable[0]
        return result

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

    def _resolve_node_mapping(
        self,
        value: object,
        folder: Path,
    ) -> dict[str, Node]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Node] = {}
        for key, node in value.items():
            if not key or not isinstance(node, dict):
                continue
            resolved = self._resolve_nodes([node], folder)
            if resolved:
                result[str(key)] = resolved[0]
        return result

    def _portable_linked_files(
        self,
        linked_folder_files: dict[str, str],
        folder: Path,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for path, node_id in linked_folder_files.items():
            if not path or not node_id:
                continue
            portable_path = to_manifest_url(str(path), folder)
            if self._is_absolute_local_path(portable_path):
                continue
            result[portable_path] = str(node_id)
        return result

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

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

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
    def _is_absolute_local_path(value: str) -> bool:
        if not value or value.startswith(("http://", "https://")):
            return False
        return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()

    @staticmethod
    def _is_inside(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _is_under_any(self, path: Path, roots: Iterable[Path]) -> bool:
        return any(self._is_inside(path, root) for root in roots)

    def _copy_into_directory(
        self,
        source: Path,
        dest_dir: Path,
        *,
        cancellation: CancellationFlag | None = None,
        created_files: list[WatchedFolderCopyResult] | None = None,
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = WatchedFolderFileStore().copy_file_transaction(
                WatchedFolderCopyRequest(source=source, folder=dest_dir, operation_id=uuid.uuid4().hex),
                cancellation=cancellation,
            )
        except OSError as exc:
            raise MeetingSyncError(f"Could not copy '{source.name}' into linked folder.") from exc
        destination = dest_dir / result.destination.relative_to(dest_dir.resolve())
        if not result.already_present and created_files is not None:
            created_files.append(result)
        if result.content is not None and content_identity(content_signature(destination)) != result.content:
            raise MediaOperationCancelled("Meeting copied resource changed during media preparation")
        return destination
