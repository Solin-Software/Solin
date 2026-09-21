"""Immutable, causally ordered edits transported by an ordinary shared folder.

The shared filesystem is a transport, not a lock service. Local archives retain
every accepted operation; an outbox is made durable before publishing an edit.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import heapq
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from solin.core.foundation.runtime_paths import RuntimePaths
from solin.core.storage.binary_files import publish_bytes_immutable, write_bytes_atomic
from solin.core.ingest.sync.lifecycle import (
    DocumentLifecycle,
    DocumentUnavailable,
    LifecycleError,
    valid_document_id,
)

VERSION = 1


def _default_state_dir() -> Path:
    return RuntimePaths.from_standard_locations().data_dir / "linked_sync"


_ID = re.compile(r"^[a-f0-9]{64}$")
_NAMESPACE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")


class JournalError(ValueError):
    """The last valid materialization must be preserved on journal errors."""


class JournalUnavailable(JournalError):
    """The linked folder cannot currently be read."""


class JournalCorrupt(JournalError):
    """An operation or local checkpoint failed validation."""


class JournalUnsupported(JournalError):
    """Another protocol version requires a coordinated application update."""


class JournalSeedConflict(JournalError):
    """Different legacy baselines require explicit resolution."""


class JournalPublicationRejected(JournalError):
    """The document is no longer authorized to publish to this folder."""


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JournalCorrupt("Journal values must be finite JSON data") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _entities(value: Any) -> dict[str, dict]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(record, dict)
        or any(not isinstance(name, str) for name in record)
        for key, record in value.items()
    ):
        raise JournalCorrupt("Expected entity IDs mapped to field records")
    _encode(value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ReplicaSnapshot:
    entities: dict[str, dict] = field(default_factory=dict)
    token: str = ""
    operation_ids: tuple[str, ...] = ()
    document_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "entities": copy.deepcopy(self.entities),
            "token": self.token,
            "operation_ids": list(self.operation_ids),
            "document_id": self.document_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> ReplicaSnapshot:
        if value.get("version") != VERSION:
            raise JournalUnsupported("Unsupported snapshot version")
        ids = value.get("operation_ids")
        if not isinstance(ids, list) or any(
            not isinstance(item, str) or not _ID.fullmatch(item) for item in ids
        ):
            raise JournalCorrupt("Invalid snapshot operation IDs")
        ids_tuple = tuple(sorted(set(ids)))
        token = _digest(ids_tuple) if ids_tuple else ""
        if value.get("token") != token:
            raise JournalCorrupt("Invalid snapshot token")
        document_id = value.get("document_id")
        if document_id is not None and not valid_document_id(document_id):
            raise JournalCorrupt("Invalid snapshot document identity")
        return cls(_entities(value.get("entities")), token, ids_tuple, document_id)


def _publish(path: Path, operation: dict) -> None:
    data = _encode(operation)
    try:
        publish_bytes_immutable(path, data)
    except FileExistsError as exc:
        raise JournalCorrupt(f"Immutable operation conflict: {path.name}") from exc


def _validate(operation: Any, namespace: str) -> dict:
    if not isinstance(operation, dict):
        raise JournalCorrupt("Operation must be an object")
    if operation.get("version") != VERSION:
        raise JournalUnsupported("Unsupported linked-folder protocol version")
    operation_id = operation.get("id")
    body = {key: value for key, value in operation.items() if key != "id"}
    if not isinstance(operation_id, str) or _digest(body) != operation_id:
        raise JournalCorrupt("Operation integrity check failed")
    if operation.get("namespace") != namespace:
        raise JournalCorrupt("Operation belongs to a different document namespace")
    if operation.get("document_id") is not None and not valid_document_id(operation["document_id"]):
        raise JournalCorrupt("Invalid operation document identity")
    dependencies = operation.get("dependencies")
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) or not _ID.fullmatch(item) for item in dependencies
    ):
        raise JournalCorrupt("Invalid causal dependencies")
    if operation.get("kind") == "seed":
        if dependencies:
            raise JournalCorrupt("Migration cannot depend on edits")
        _entities(operation.get("entities"))
    elif operation.get("kind") == "edit":
        _entities(operation.get("patches"))
        for key in ("deleted", "restored"):
            if not isinstance(operation.get(key), list) or any(
                not isinstance(item, str) or not item for item in operation[key]
            ):
                raise JournalCorrupt(f"Invalid {key} IDs")
        removed = operation.get("removed_fields")
        if not isinstance(removed, dict) or any(
            not isinstance(key, str)
            or not isinstance(fields, list)
            or any(not isinstance(name, str) for name in fields)
            for key, fields in removed.items()
        ):
            raise JournalCorrupt("Invalid removed fields")
    else:
        raise JournalCorrupt("Unknown operation kind")
    return operation


class JournalReplica:
    def __init__(
        self,
        folder: Path,
        namespace: str,
        *,
        state_dir: Path | None = None,
        actor_id: str | None = None,
        publication_guard: Callable[[str], None] | None = None,
    ):
        if not _NAMESPACE.fullmatch(namespace) or namespace in {".", ".."}:
            raise ValueError("Invalid journal namespace")
        self.folder = Path(os.path.abspath(folder))
        self.namespace = namespace
        self.operations_dir = self.folder / ".solin_sync" / namespace / "operations"
        root = (
            Path(state_dir)
            if state_dir is not None
            else _default_state_dir()
        )
        self._lifecycle = DocumentLifecycle(self.folder, namespace, root)
        self.document_id = self._lifecycle.document_id
        self.state_dir = self._lifecycle.state_dir
        self.archive_dir = self.state_dir / "operations"
        self.outbox_dir = self.state_dir / "outbox"
        self.actor_id = actor_id or uuid.uuid4().hex
        self._publication_guard = publication_guard
        self._required_document_id: str | None = None
        self._operations: dict[str, dict] = {}
        self._local_operations: dict[str, dict] = {}
        self._signatures: dict[Path, tuple[int, int, int]] = {}
        self.pending_count = 0
        self.waiting_count = 0
        self._projection_ids: tuple[str, ...] | None = None
        self._projection: ReplicaSnapshot | None = None
        self._lock = RLock()
        self._stage_lock = RLock()
        self._aliases: dict[str, str] = {}
        if self.document_id:
            self._switch_document(self.state_dir, self.document_id)

    @property
    def has_previous_document(self) -> bool:
        return bool(self._lifecycle.supersedes)

    def _switch_document(self, previous_state: Path, previous_document: str | None) -> None:
        self.document_id = self._lifecycle.document_id
        self.state_dir = self._lifecycle.state_dir
        self.archive_dir = self.state_dir / "operations"
        self.outbox_dir = self.state_dir / "outbox"
        self._operations = {}
        self._local_operations = {}
        self._signatures = {}
        self._projection = None
        self._projection_ids = None
        self._aliases = {}
        aliases_path = self.state_dir / "aliases.json"
        if aliases_path.exists():
            try:
                self._aliases = json.loads(aliases_path.read_bytes())
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise JournalCorrupt("Invalid provisional operation aliases") from exc
            if not isinstance(self._aliases, dict) or any(
                not valid_document_id(key) or not valid_document_id(value)
                for key, value in self._aliases.items()
            ):
                raise JournalCorrupt("Invalid provisional operation aliases")
        adoption_dir = self._lifecycle.pending_adoption_dir
        if adoption_dir is not None and self.document_id:
            self._adopt_provisional(adoption_dir)
            self._lifecycle.complete_adoption()

    def _adopt_provisional(self, previous_state: Path) -> None:
        operations = self._scan(previous_state / "outbox")
        operations.update(self._scan(previous_state / "operations"))
        pending = dict(operations)
        while pending:
            ready = [key for key, operation in pending.items()
                     if all(dependency in self._aliases for dependency in operation["dependencies"])]
            if not ready:
                raise JournalCorrupt("Incomplete provisional causal history")
            for key in sorted(ready):
                operation = pending.pop(key)
                if operation.get("document_id") is not None:
                    raise JournalCorrupt("Provisional history contains another document")
                body = {name: copy.deepcopy(value) for name, value in operation.items() if name != "id"}
                body["document_id"] = self.document_id
                body["dependencies"] = sorted(self._aliases[dependency] for dependency in operation["dependencies"])
                translated = {**body, "id": _digest(body)}
                _publish(self.outbox_dir / f"{translated['id']}.json", translated)
                self._local_operations[translated["id"]] = translated
                self._aliases[key] = translated["id"]
        write_bytes_atomic(self.state_dir / "aliases.json", _encode(self._aliases))
        for path in (previous_state / "intents").glob("*.json"):
            target = self.state_dir / "intents" / path.name
            if not target.exists():
                write_bytes_atomic(target, path.read_bytes())

    def _refresh_document(self, *, local_only: bool = False) -> None:
        previous_state, previous_document = self.state_dir, self.document_id
        try:
            if local_only:
                self._lifecycle.refresh_binding()
            else:
                self._lifecycle.refresh(preferred_document_id=self._required_document_id)
        except DocumentUnavailable as exc:
            raise JournalUnavailable(str(exc)) from exc
        except LifecycleError as exc:
            raise JournalCorrupt(str(exc)) from exc
        if self._lifecycle.state_dir != previous_state:
            with self._stage_lock:
                self._switch_document(previous_state, previous_document)
        elif self._lifecycle.pending_adoption_dir is not None:
            with self._stage_lock:
                self._adopt_provisional(self._lifecycle.pending_adoption_dir)
                self._lifecycle.complete_adoption()

    def initialize_document(self) -> str:
        with self._lock:
            return self._initialize_document()

    def require_document(self, document_id: str) -> None:
        """Bind reads to the document selected by an external activation marker."""
        if not valid_document_id(document_id):
            raise JournalCorrupt("Invalid activated document identity")
        with self._lock:
            previous = self._required_document_id
            self._required_document_id = document_id
            try:
                self._refresh_document()
            except BaseException:  # noqa: BLE001 - Restore selection on failed activation validation.
                self._required_document_id = previous
                raise

    def is_retired(self, document_id: str) -> bool:
        return document_id in self._lifecycle.binding["retired"]

    @property
    def has_retired_documents(self) -> bool:
        return bool(self._lifecycle.binding["retired"])

    def _authorize_publication(self) -> None:
        if self._publication_guard is None or self.document_id is None:
            return
        try:
            self._publication_guard(self.document_id)
        except JournalError:
            raise
        except (OSError, ValueError) as exc:
            raise JournalPublicationRejected(str(exc)) from exc

    def _initialize_document(self, *, migration_id: str | None = None, reset: bool = False) -> str:
        previous_state, previous_document = self.state_dir, self.document_id
        try:
            document_id = self._lifecycle.initialize(migration_id=migration_id, reset=reset)
        except LifecycleError as exc:
            raise JournalCorrupt(str(exc)) from exc
        with self._stage_lock:
            if self._lifecycle.state_dir != previous_state:
                self._switch_document(previous_state, previous_document)
        self._authorize_publication()
        self._lifecycle.publish()
        return document_id

    def reset_document(self) -> str:
        with self._lock:
            previous_required = self._required_document_id
            self._required_document_id = None
            try:
                return self._initialize_document(reset=True)
            except BaseException:  # noqa: BLE001 - Restore the previous selector on failed reset.
                self._required_document_id = previous_required
                raise

    def retire_binding(
        self,
        document_id: str | None = None,
        *,
        include_known: bool = True,
    ) -> None:
        with self._lock, self._stage_lock:
            previous_state, previous_document = self.state_dir, self.document_id
            try:
                self._lifecycle.retire_binding(document_id, include_known=include_known)
            except LifecycleError as exc:
                raise JournalCorrupt(str(exc)) from exc
            self._required_document_id = None
            self._switch_document(previous_state, previous_document)

    @contextmanager
    def retiring_binding(
        self,
        document_id: str | None = None,
        *,
        include_known: bool = True,
    ):
        """Retire before deleting a folder and restore on unsuccessful deletion.

        The durable archive remains available even if filesystem removal
        partially succeeds. A failure therefore retains the known organization.
        """
        with self._lock, self._stage_lock:
            checkpoint = copy.deepcopy(self._lifecycle.binding)
            checkpoint_required = self._required_document_id
            self.retire_binding(document_id, include_known=include_known)
            try:
                yield
            except BaseException:  # noqa: BLE001 - Restore the durable binding, then re-raise.
                previous_state, previous_document = self.state_dir, self.document_id
                self._lifecycle.restore_binding(checkpoint)
                self._required_document_id = checkpoint_required
                self._switch_document(previous_state, previous_document)
                raise

    def rebind_folder(self, new_folder: Path) -> None:
        with self.rebinding_folder(new_folder):
            pass

    @contextmanager
    def rebinding_folder(self, new_folder: Path):
        """Move the binding, rolling it back if a dependent rename step fails."""
        with self._lock, self._stage_lock:
            folder = Path(new_folder).resolve()
            previous_folder, previous_operations = self.folder, self.operations_dir
            try:
                with self._lifecycle.rebinding_folder(folder):
                    self.folder = folder
                    self.operations_dir = folder / ".solin_sync" / self.namespace / "operations"
                    try:
                        yield
                    except BaseException:  # noqa: BLE001 - Roll back in-memory paths, then re-raise.
                        self.folder, self.operations_dir = previous_folder, previous_operations
                        raise
            except LifecycleError as exc:
                raise JournalCorrupt(str(exc)) from exc

    def _normalize_base(self, base: ReplicaSnapshot) -> ReplicaSnapshot:
        if base.document_id == self.document_id:
            return base
        if base.document_id is None and self.document_id:
            if any(key not in self._aliases for key in base.operation_ids):
                raise JournalCorrupt("Provisional snapshot does not belong to this document")
            ids = tuple(sorted(self._aliases[key] for key in base.operation_ids))
            return ReplicaSnapshot(base.entities, _digest(ids) if ids else "", ids, self.document_id)
        raise JournalCorrupt("The linked document was replaced; preserve the old edit separately")

    def _scan(self, directory: Path) -> dict[str, dict]:
        found = {}
        if not directory.exists():
            return found
        for path in directory.iterdir():
            if path.name.startswith(".") or path.suffix.lower() != ".json":
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise JournalCorrupt("Operation file escapes its journal directory")
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue  # Another local process may have archived this outbox file.
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._operations.get(path.stem)
            if cached is not None and self._signatures.get(path) == signature:
                found[path.stem] = cached
                continue
            try:
                operation = _validate(json.loads(path.read_bytes()), self.namespace)
            except FileNotFoundError:
                continue
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise JournalCorrupt(f"Incomplete or invalid operation: {path.name}") from exc
            operation_id = operation["id"]
            if cached is not None and cached != operation:
                raise JournalCorrupt("Immutable operation changed")
            found[operation_id] = operation
            self._signatures[path] = signature
        return found

    def _load(self, *, offline: bool = False) -> dict[str, dict]:
        try:
            self._refresh_document()
        except OSError as exc:
            if not offline:
                raise JournalUnavailable(str(exc)) from exc
        # Publication archives an outbox file before removing it. This scan
        # order sees at least one copy even when another process publishes it.
        operations = self._scan(self.outbox_dir)
        archived = self._scan(self.archive_dir)
        operations.update(archived)
        try:
            if not self.folder.is_dir():
                raise OSError("Linked folder is unavailable")
            if not self.operations_dir.resolve().is_relative_to(self.folder.resolve()):
                raise JournalCorrupt("Journal directory escapes its linked folder")
            remote = self._scan(self.operations_dir) if self.document_id else {}
        except OSError as exc:
            if not offline:
                raise JournalUnavailable(str(exc)) from exc
            remote = {}
        remote = {key: value for key, value in remote.items()
                  if value.get("document_id") == self.document_id}
        operations.update(remote)
        # Validate the entire batch before persisting any of its contents.
        self._materialize(operations, copy_result=False)
        for operation_id, operation in remote.items():
            if operation_id not in archived:
                _publish(self.archive_dir / f"{operation_id}.json", operation)
        self._operations = operations
        return operations

    def _flush(self) -> None:
        with self._stage_lock:
            pending = self._scan(self.outbox_dir)
            self.pending_count = len(pending)
        if not self.document_id:
            return
        if not self.folder.is_dir():
            return
        self._authorize_publication()
        if not self._lifecycle.publish():
            self.pending_count += 1
            return
        if not self.operations_dir.resolve().is_relative_to(self.folder.resolve()):
            raise JournalCorrupt("Journal directory escapes its linked folder")
        for operation_id, operation in pending.items():
            try:
                self._authorize_publication()
                _publish(self.operations_dir / f"{operation_id}.json", operation)
            except OSError:
                return  # The durable outbox remains the retry source.
            _publish(self.archive_dir / f"{operation_id}.json", operation)
            (self.outbox_dir / f"{operation_id}.json").unlink(missing_ok=True)
            with self._stage_lock:
                self.pending_count -= 1

    def read(self, seed: dict[str, dict] | None = None) -> ReplicaSnapshot:
        with self._lock:
            return self._read(seed)

    def read_verified_seed(self, seed: dict[str, dict]) -> ReplicaSnapshot:
        """Read an activated migration only if its immutable baseline agrees."""

        with self._lock:
            operations = self._load()
            entities = _entities(seed)
            seeds = [item for item in operations.values() if item["kind"] == "seed"]
            if not seeds or any(item["entities"] != entities for item in seeds):
                raise JournalSeedConflict(
                    "Legacy baseline differs from the migrated document"
                )
            self._flush()
            return self._materialize(operations)

    def read_local(self) -> ReplicaSnapshot:
        """Read the durable known document without consulting cloud metadata.

        Explicit conflict resolution can preserve this baseline even when
        unrelated remote descriptors make ordinary refresh fail closed.
        """
        with self._lock:
            operations = self._scan(self.outbox_dir)
            operations.update(self._scan(self.archive_dir))
            if any(operation.get("document_id") != self.document_id
                   for operation in operations.values()):
                raise JournalCorrupt("Local history belongs to another document")
            return self._materialize(operations)

    def _read(self, seed: dict[str, dict] | None) -> ReplicaSnapshot:
        if seed is not None and not self.document_id:
            self._initialize_document(migration_id=_digest([self.namespace, _entities(seed)]))
        operations = self._load()
        if seed is not None:
            entities = _entities(seed)
            seeds = [item for item in operations.values() if item["kind"] == "seed"]
            if any(item["entities"] != entities for item in seeds):
                raise JournalSeedConflict("Legacy baseline differs from the migrated document")
            if not seeds:
                body = {
                    "version": VERSION,
                    "namespace": self.namespace,
                    "document_id": self.document_id,
                    "kind": "seed",
                    "dependencies": [],
                    "entities": entities,
                }
                operation = {**body, "id": _digest(body)}
                self._persist(operation)
                operations[operation["id"]] = operation
        self._flush()
        return self._materialize(operations)

    def _persist(self, operation: dict) -> None:
        with self._stage_lock:
            _publish(self.outbox_dir / f"{operation['id']}.json", operation)
            self._local_operations[operation["id"]] = operation
            self.pending_count += 1

    def stage(
        self,
        base: ReplicaSnapshot,
        entities: dict[str, dict],
        *,
        restore_ids: set[str] | None = None,
    ) -> ReplicaSnapshot:
        """Durably accept an intent without touching the linked-folder transport."""
        # A background reader may wait on cloud hydration while holding _lock.
        # Local intentions only need immutable cached/local causal history.
        with self._stage_lock:
            return self._commit(base, entities, restore_ids=restore_ids, publish=False)

    def commit(
        self,
        base: ReplicaSnapshot,
        entities: dict[str, dict],
        *,
        restore_ids: set[str] | None = None,
    ) -> ReplicaSnapshot:
        with self._lock:
            return self._commit(base, entities, restore_ids=restore_ids, publish=True)

    def _commit(
        self,
        base: ReplicaSnapshot,
        entities: dict[str, dict],
        *,
        restore_ids: set[str] | None,
        publish: bool,
    ) -> ReplicaSnapshot:
        desired = _entities(entities)
        if publish:
            operations = self._load(offline=True)
        else:
            self._refresh_document(local_only=True)
            operations = {**self._operations, **self._local_operations}
            if any(item not in operations for item in base.operation_ids):
                # Restart recovery reads the durable local archive only. Never
                # scan shared paths or replace a background reader's projection.
                operations.update(self._scan(self.outbox_dir))
                operations.update(self._scan(self.archive_dir))
                self._local_operations.update(operations)
        base = self._normalize_base(base)
        if any(item not in operations for item in base.operation_ids):
            raise JournalCorrupt("Local causal history is missing; preserve the previous snapshot")
        patches = {}
        removed_fields = {}
        restored = sorted((restore_ids or set()) & desired.keys())
        for entity_id, record in desired.items():
            previous = base.entities.get(entity_id, {})
            changed = {
                name: value
                for name, value in record.items()
                if name not in previous or previous[name] != value
            }
            if entity_id not in base.entities or entity_id in restored:
                patches[entity_id] = record
            elif changed:
                patches[entity_id] = changed
            missing = sorted(previous.keys() - record.keys())
            if missing:
                removed_fields[entity_id] = missing
        deleted = sorted(base.entities.keys() - desired.keys())
        staged_ids = set(base.operation_ids)
        if patches or removed_fields or deleted or restored:
            # A frontier bounds dependency growth without losing causal context.
            dependencies = set(base.operation_ids)
            for operation_id in base.operation_ids:
                dependencies.difference_update(operations[operation_id]["dependencies"])
            body = {
                "version": VERSION,
                "namespace": self.namespace,
                "document_id": self.document_id,
                "kind": "edit",
                "actor": self.actor_id,
                "nonce": uuid.uuid4().hex,
                "dependencies": sorted(dependencies),
                "patches": patches,
                "removed_fields": removed_fields,
                "deleted": deleted,
                "restored": restored,
            }
            operation = {**body, "id": _digest(body)}
            self._persist(operation)
            operations[operation["id"]] = operation
            staged_ids.add(operation["id"])
        if publish:
            self._flush()
        else:
            # The GUI has only rendered its own base. Incorporating archived
            # remote inserts here would make the next local diff delete nodes
            # that have not actually been presented to the user yet.
            # A local edit is causally after every operation in its GUI base,
            # so replaying the complete journal can only yield the requested
            # field values. The one exception is reusing a tombstoned ID.
            added_ids = desired.keys() - base.entities.keys() - set(restored)
            if added_ids:
                deleted_ids = {
                    entity_id
                    for key in base.operation_ids
                    for entity_id in operations[key].get("deleted", [])
                }
                for entity_id in added_ids & deleted_ids:
                    desired.pop(entity_id, None)
            ids = tuple(sorted(staged_ids))
            return ReplicaSnapshot(desired, _digest(ids) if ids else "", ids, self.document_id)
        return self._materialize(operations)

    def _materialize(
        self, operations: dict[str, dict], *, copy_result: bool = True
    ) -> ReplicaSnapshot:
        projection_ids = tuple(sorted(operations))
        if projection_ids == self._projection_ids and self._projection is not None:
            return copy.deepcopy(self._projection) if copy_result else self._projection
        seeds = [item for item in operations.values() if item["kind"] == "seed"]
        if len({_digest(item["entities"]) for item in seeds}) > 1:
            raise JournalSeedConflict("Conflicting legacy migration baselines")
        # Bitsets keep long offline histories compact; Python sets containing
        # every ancestor grow quadratically in object references.
        ancestors: dict[str, int] = {}
        bits = {key: 1 << index for index, key in enumerate(projection_ids)}
        dependents: dict[str, list[str]] = {}
        remaining = {}
        ready = []
        for operation_id, operation in operations.items():
            dependencies = set(operation["dependencies"])
            remaining[operation_id] = len(dependencies)
            if not dependencies:
                heapq.heappush(ready, operation_id)
            for dependency in dependencies:
                dependents.setdefault(dependency, []).append(operation_id)
        ordered = []
        while ready:
            operation_id = heapq.heappop(ready)
            operation = operations[operation_id]
            context = 0
            for dependency in operation["dependencies"]:
                context |= ancestors[dependency] | bits[dependency]
            ancestors[operation_id] = context
            ordered.append(operation)
            for child in dependents.get(operation_id, []):
                remaining[child] -= 1
                if not remaining[child]:
                    heapq.heappush(ready, child)
        self.waiting_count = len(operations) - len(ancestors)
        # Each register retains concurrent writers; causally later edits replace
        # earlier writers. The content-derived operation ID resolves concurrency.
        registers: dict[tuple[str, str], dict[str, tuple[bool, Any]]] = {}
        existence: set[str] = set()
        deletions: dict[str, set[str]] = {}
        restores: dict[str, set[str]] = {}
        for operation in ordered:
            operation_id = operation["id"]
            patches = operation.get("entities", operation.get("patches", {}))
            existence.update(patches)
            for entity_id in operation.get("deleted", []):
                deletions.setdefault(entity_id, set()).add(operation_id)
            for entity_id in operation.get("restored", []):
                restores.setdefault(entity_id, set()).add(operation_id)
            changes = {
                (entity_id, name): (True, value)
                for entity_id, record in patches.items()
                for name, value in record.items()
            }
            changes.update(
                {
                    (entity_id, name): (False, None)
                    for entity_id, names in operation.get("removed_fields", {}).items()
                    for name in names
                }
            )
            for key, value in changes.items():
                writers = registers.setdefault(key, {})
                for writer in list(writers):
                    if ancestors[operation_id] & bits[writer]:
                        del writers[writer]
                writers[operation_id] = value
        entities: dict[str, dict] = {}
        for entity_id in existence:
            active_deletes = deletions.get(entity_id, set())
            if any(
                not any(
                    ancestors[restore] & bits[deletion]
                    for restore in restores.get(entity_id, set())
                )
                for deletion in active_deletes
            ):
                continue
            entities[entity_id] = {}
        for (entity_id, name), writers in registers.items():
            if entity_id in entities:
                winner = max(
                    writers, key=lambda writer: (operations[writer]["kind"] != "seed", writer)
                )
                present, value = writers[winner]
                if present:
                    entities[entity_id][name] = copy.deepcopy(value)
        ids = tuple(sorted(ancestors))
        snapshot = ReplicaSnapshot(entities, _digest(ids) if ids else "", ids, self.document_id)
        self._projection_ids = projection_ids
        self._projection = copy.deepcopy(snapshot)
        return snapshot
