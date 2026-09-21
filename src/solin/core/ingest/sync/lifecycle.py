"""Explicit document identities and local folder bindings.

Missing cloud metadata never establishes a new document. Unbound folders keep
provisional local intentions until initialization or an existing descriptor
arrives. An explicit reset supersedes prior identities without deleting history.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from solin.core.storage.binary_files import publish_bytes_immutable, write_bytes_atomic


VERSION = 1
_DOCUMENT_ID = re.compile(r"^[a-f0-9]{64}$")


class LifecycleError(ValueError):
    """Invalid or incompatible document lifecycle metadata."""


class DocumentConflict(LifecycleError):
    """Unrelated authoritative identities require explicit resolution."""


class DocumentUnavailable(LifecycleError):
    """The selected descriptor has not arrived from the transport yet."""


def valid_document_id(value: object) -> bool:
    return isinstance(value, str) and bool(_DOCUMENT_ID.fullmatch(value))


def _encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("Incomplete document identity metadata") from exc
    if not isinstance(value, dict):
        raise LifecycleError("Document identity metadata must be an object")
    return value


def _publish(path: Path, value: dict) -> None:
    data = _encode(value)
    try:
        publish_bytes_immutable(path, data)
    except FileExistsError as exc:
        raise LifecycleError("Immutable document descriptor changed") from exc


def _descriptor(value: dict, namespace: str) -> dict:
    if value.get("version") != VERSION or value.get("namespace") != namespace:
        raise LifecycleError("Unsupported document identity version or namespace")
    if not valid_document_id(value.get("document_id")):
        raise LifecycleError("Invalid document identity")
    predecessors = value.get("supersedes")
    if not isinstance(predecessors, list) or any(not valid_document_id(key) for key in predecessors):
        raise LifecycleError("Invalid predecessor identities")
    if value["document_id"] in predecessors:
        raise LifecycleError("A document identity cannot supersede itself")
    return value


class DocumentLifecycle:
    def __init__(self, folder: Path, namespace: str, root: Path):
        self.folder, self.namespace, self.root = folder, namespace, root
        self._cache: dict[Path, tuple[tuple[int, int, int], dict]] = {}
        self.binding_path = self._binding_path(folder)
        self.binding = self._load_binding()
        self.storage_dir = root / "replicas" / self.binding["storage_id"]
        self.descriptor_dir = self.storage_dir / "descriptors"
        self.descriptors: dict[str, dict] = {}
        self.pending = False

    def _binding_path(self, folder: Path) -> Path:
        key = hashlib.sha256(_encode({"folder": os.path.normcase(str(folder)), "namespace": self.namespace})).hexdigest()
        return self.root / "bindings" / f"{key}.json"

    def _load_binding(self) -> dict:
        if self.binding_path.exists():
            value = self._read_cached(self.binding_path)
            if (value.get("version") != VERSION or not valid_document_id(value.get("storage_id"))
                    or not isinstance(value.get("provisional_id"), str)
                    or not re.fullmatch(r"[a-zA-Z0-9_-]+", value["provisional_id"])
                    or not isinstance(value.get("retired"), list)
                    or any(not valid_document_id(key) for key in value["retired"])
                    or (value.get("document_id") is not None and not valid_document_id(value["document_id"]))):
                raise LifecycleError("Invalid local document binding")
            return value
        return {"version": VERSION, "storage_id": self.binding_path.stem,
                "provisional_id": "initial", "document_id": None, "retired": []}

    def _read_cached(self, path: Path) -> dict:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == signature:
            return deepcopy(cached[1])
        value = _read(path)
        self._cache[path] = (signature, deepcopy(value))
        return value

    @property
    def document_id(self) -> str | None:
        return self.binding["document_id"]

    @property
    def provisional_dir(self) -> Path:
        return self.storage_dir / "provisional" / self.binding["provisional_id"]

    @property
    def state_dir(self) -> Path:
        return (self.root / "documents" / self.namespace / self.document_id
                if self.document_id else self.provisional_dir)

    @property
    def shared_dir(self) -> Path:
        return self.folder / ".solin_sync" / self.namespace / "documents"

    @property
    def supersedes(self) -> tuple[str, ...]:
        descriptor = self.descriptors.get(self.document_id or "", {})
        return tuple(descriptor.get("supersedes", []))

    @property
    def pending_adoption_dir(self) -> Path | None:
        identifier = self.binding.get("adopt_provisional")
        if not identifier:
            return None
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", identifier):
            raise LifecycleError("Invalid provisional adoption source")
        return self.storage_dir / "provisional" / identifier

    def complete_adoption(self) -> None:
        self.binding.pop("adopt_provisional", None)
        self._save_binding()

    def _save_binding(self) -> None:
        write_bytes_atomic(self.binding_path, _encode(self.binding))

    def refresh_binding(self) -> None:
        """Observe local retirement or replacement without cloud filesystem I/O."""
        if self.binding_path.exists():
            self.binding = self._load_binding()

    def restore_binding(self, binding: dict) -> None:
        """Restore a local checkpoint after an unsuccessful folder operation."""
        write_bytes_atomic(self.binding_path, _encode(binding))
        self.binding = deepcopy(binding)

    def _scan(self, directory: Path) -> dict[str, dict]:
        if not directory.exists():
            return {}
        result = {}
        for path in directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise LifecycleError("Document descriptor escapes its directory")
            value = _descriptor(self._read_cached(path), self.namespace)
            if path.stem != value["document_id"]:
                raise LifecycleError("Document descriptor filename does not match identity")
            result[path.stem] = value
        return result

    def refresh(
        self,
        *,
        allow_conflict: bool = False,
        preferred_document_id: str | None = None,
    ) -> str | None:
        if not self.folder.is_dir():
            raise OSError("Linked folder is unavailable")
        if not self.shared_dir.resolve().is_relative_to(self.folder.resolve()):
            raise LifecycleError("Document identity directory escapes linked folder")
        # Re-read the local binding so an explicit retirement in another local
        # process is not mistaken for a temporary transport disappearance.
        self.refresh_binding()
        known = self._scan(self.descriptor_dir)
        received = self._scan(self.shared_dir)
        for key, descriptor in received.items():
            if key in known and known[key] != descriptor:
                raise LifecycleError("Immutable document descriptor changed")
        combined = known | received
        superseded = {key for value in combined.values() for key in value["supersedes"]}
        retired = set(self.binding["retired"])
        heads = combined.keys() - superseded - retired
        if combined and not heads and not retired:
            raise LifecycleError("Document identity graph contains a cycle")
        if preferred_document_id is not None:
            if not valid_document_id(preferred_document_id):
                raise LifecycleError("Invalid preferred document identity")
            if preferred_document_id in retired:
                raise DocumentConflict("The activated document was retired locally")
            if preferred_document_id not in combined:
                raise DocumentUnavailable(
                    "The activated document descriptor is not available yet"
                )
            if preferred_document_id not in heads:
                raise DocumentConflict("The activated document was superseded")
        elif len(heads) > 1 and not allow_conflict:
            raise DocumentConflict("Conflicting document identities; select or reset the linked document explicitly")
        for key, descriptor in received.items():
            if key not in known:
                _publish(self.descriptor_dir / f"{key}.json", descriptor)
        self.descriptors = combined
        selected = preferred_document_id or (next(iter(heads)) if len(heads) == 1 else None)
        if selected != self.document_id:
            if self.document_id is None and selected:
                self.binding["adopt_provisional"] = self.binding["provisional_id"]
            else:
                self.binding.pop("adopt_provisional", None)
            self.binding["document_id"] = selected
            self._save_binding()
        return selected

    def initialize(self, *, migration_id: str | None = None, reset: bool = False) -> str:
        self.refresh(allow_conflict=reset)
        if self.document_id and not reset:
            return self.document_id
        ancestry = {key for descriptor in self.descriptors.values() for key in descriptor["supersedes"]}
        predecessors = sorted(self.descriptors.keys() | ancestry | set(self.binding["retired"])) if reset or self.binding["retired"] else []
        # The migration caller supplies a content-derived ID, ensuring identical
        # legacy baselines initialized on multiple computers agree exactly.
        document_id = migration_id if migration_id and not predecessors else hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        descriptor = {"version": VERSION, "namespace": self.namespace,
                      "document_id": document_id, "supersedes": predecessors}
        _publish(self.descriptor_dir / f"{document_id}.json", descriptor)
        self.descriptors[document_id] = descriptor
        if self.document_id is None and not reset:
            self.binding["adopt_provisional"] = self.binding["provisional_id"]
        else:
            self.binding.pop("adopt_provisional", None)
        self.binding["document_id"] = document_id
        self._save_binding()
        self.pending = True
        return document_id

    def publish(self) -> bool:
        if not self.document_id:
            return False
        descriptor = self.descriptors.get(self.document_id)
        if descriptor is None:
            descriptor = _descriptor(self._read_cached(self.descriptor_dir / f"{self.document_id}.json"), self.namespace)
        try:
            path = self.shared_dir / f"{self.document_id}.json"
            if path.exists():
                if self._read_cached(path) != descriptor:
                    raise LifecycleError("Immutable document descriptor changed")
            else:
                _publish(path, descriptor)
        except OSError:
            self.pending = True
            return False
        self.pending = False
        return True

    def retire_binding(
        self,
        document_id: str | None = None,
        *,
        include_known: bool = True,
    ) -> None:
        if document_id is not None and not valid_document_id(document_id):
            raise LifecycleError("Invalid retired document identity")
        known = set(self.descriptors) if include_known else set()
        self.binding["retired"] = sorted(set(self.binding["retired"]) | known
                                         | ({self.document_id} if self.document_id else set())
                                         | ({document_id} if document_id else set()))
        self.binding["document_id"] = None
        self.binding.pop("adopt_provisional", None)
        self.binding["provisional_id"] = uuid.uuid4().hex
        self._save_binding()

    def rebind_folder(self, folder: Path) -> None:
        destination = self._binding_path(folder)
        if destination == self.binding_path:
            return
        if destination.exists():
            existing = _read(destination)
            if existing.get("storage_id") != self.binding["storage_id"]:
                raise DocumentConflict("The destination already has a different local document binding")
        old_binding = deepcopy(self.binding)
        old_binding["retired"] = sorted(set(old_binding["retired"]) | set(self.descriptors)
                                         | ({self.document_id} if self.document_id else set()))
        old_binding["document_id"] = None
        old_binding.pop("adopt_provisional", None)
        old_binding["provisional_id"] = uuid.uuid4().hex
        write_bytes_atomic(destination, _encode(self.binding))
        write_bytes_atomic(self.binding_path, _encode(old_binding))
        self.folder, self.binding_path = folder, destination

    @contextmanager
    def rebinding_folder(self, folder: Path):
        """Restore both local bindings if a dependent rename step fails."""
        previous_folder, previous_path = self.folder, self.binding_path
        previous_binding = deepcopy(self.binding)
        destination = self._binding_path(folder)
        checkpoints = {
            path: path.read_bytes() if path.exists() else None
            for path in {previous_path, destination}
        }
        try:
            self.rebind_folder(folder)
            yield
        except BaseException:  # noqa: BLE001 - Restore both binding checkpoints, then re-raise.
            self.folder, self.binding_path = previous_folder, previous_path
            self.binding = previous_binding
            for path, payload in checkpoints.items():
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, payload)
            raise
