"""Playlist adapter for the shared linked-folder operation journal."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import logging
import os
from pathlib import Path
import threading
from typing import Any
import uuid

from solin.core.ingest.manifest import MANIFEST_REPOSITORY
from solin.core.ingest.sync.journal import JournalReplica, ReplicaSnapshot, JournalError
from solin.core.ingest.sync.discovery import (
    AUXILIARY, DISCOVERED, RESOURCE, SUPPRESSED, REPEATABLE,
    reconcile_discoveries, record_discovery_edits, suppressed_resources, suppression_record,
)
from solin.core.ingest.sync.resources import (
    automatic_occurrence_id, portable_resource_key, recover_file, retire_file, resource_identity_key,
)
from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes
from solin.core.ingest.sync.materialization import portable_playlist_url
from solin.core.playlists.items import create_playlist_item
from solin.core.media.identity import PLAYLIST_MEDIA_OCCURRENCES
from solin.core.playlists.tree_editing import build_playlist_tree, flatten_playlist_tree
from solin.core.storage.json_repository import JsonFileRepository
from solin.core.storage.binary_files import write_bytes_atomic


log = logging.getLogger(__name__)
STATE = "__linked_sync"
INTENT = "__linked_intent"
_SOURCES = "$source:"
_REGISTRY_LOCK = threading.RLock()
_SERVICES: dict[str, LinkedPlaylistSync] = {}


def playlist_sync(folder: str | Path, *, refresh_binding: bool = False) -> LinkedPlaylistSync:
    key = os.path.normcase(os.path.abspath(folder))
    with _REGISTRY_LOCK:
        service = _SERVICES.get(key)
        if (service is None or (refresh_binding and service.folder != Path(folder)
                               and Path(folder).is_dir())):
            service = LinkedPlaylistSync(Path(folder))
            _SERVICES[key] = service
        return service


def register_playlist_sync(folder: Path, service: LinkedPlaylistSync) -> None:
    """Route future reads of a renamed folder to its existing replica."""
    with _REGISTRY_LOCK:
        _SERVICES[os.path.normcase(os.path.abspath(folder))] = service


def _nodes(playlist: dict) -> list[dict]:
    def convert(node):
        ref = deepcopy(node["ref"])
        media_type = ref.pop("type", None)
        for field in ("section_id", "subsection_id", "parent_id", "position", "slot_order"):
            ref.pop(field, None)
        return {
            **ref,
            "type": node["type"],
            "media_type": media_type,
            "children": [convert(child) for child in node.get("children", [])],
        }
    return [convert(node) for node in build_playlist_tree(playlist)]


def _playlist(entities: dict[str, dict]) -> dict:
    def convert(node):
        ref = {key: deepcopy(value) for key, value in node.items()
               if key not in {"type", "children", "media_type"}}
        if node["type"] == "media":
            ref["type"] = node.get("media_type") or "image"
        return {"id": node["id"], "type": node["type"], "ref": ref,
                "children": [convert(child) for child in node.get("children", [])]}
    tree = rebuild_nodes({key: value for key, value in entities.items()
                          if not value.get(AUXILIARY)})
    result: dict[str, Any] = {}
    flatten_playlist_tree(result, [convert(node) for node in tree])
    return result


class LinkedPlaylistSync:
    def __init__(self, folder: Path, *, state_dir: Path | None = None,
                 actor_id: str | None = None):
        self.folder = folder
        self.replica = JournalReplica(folder, "playlist", state_dir=state_dir, actor_id=actor_id)
        self.lock = threading.RLock()
        # Only local intent files use this lock. Cloud reads and conversions
        # must never prevent the GUI from durably staging its next edit.
        self.intent_lock = threading.RLock()
        self.resource_errors: list[str] = []
        self._migration_verified = False

    def entities(self, playlist: dict, baseline: dict[str, dict] | None = None,
                 *, local_intent: bool = False) -> dict[str, dict]:
        prepared = deepcopy(playlist)
        for index, item in enumerate(prepared.get("items", [])):
            item[REPEATABLE] = PLAYLIST_MEDIA_OCCURRENCES.allows_repeat(item)
            url = str(item.get("url") or "")
            if url:
                if local_intent:
                    # These URLs were validated when the view/copy was loaded.
                    # GUI intent staging must not resolve/stat cloud files.
                    if url.startswith(("http://", "https://")):
                        item[RESOURCE] = url
                    else:
                        path = Path(url)
                        if path.is_absolute():
                            try:
                                path = path.relative_to(self.folder)
                            except ValueError:
                                # Accept organization now; the durable local
                                # intent retains the source for background
                                # adoption. Shared state never needs its
                                # machine-specific path.
                                item.pop("url", None)
                                item.pop(RESOURCE, None)
                                item.setdefault("id", str(uuid.uuid4()))
                                continue
                        if ".." in path.parts:
                            raise ValueError("Resource escapes linked folder")
                        item[RESOURCE] = path.as_posix()
                else:
                    item[RESOURCE] = portable_resource_key(url, self.folder)
                item["url"] = item[RESOURCE]
            item.setdefault("id", automatic_occurrence_id(url or f"item:{index}"))
        return flatten_nodes(_nodes(prepared), baseline)

    def read(self, legacy: dict | None = None) -> ReplicaSnapshot:
        current = self.replica.read()
        if self.replica.has_previous_document:
            return current
        if "$migration:playlist" in current.entities and self._migration_verified:
            return current
        original_bytes = None
        if legacy is None:
            legacy, original_bytes = MANIFEST_REPOSITORY.load_frozen(self.folder, strict=True)
        if legacy.get("version", 1) != 1:
            raise ValueError("Unsupported linked-folder manifest version; update every terminal")
        seed = None
        if legacy and ("playlist" in legacy or legacy.get("processed")):
            prepared = deepcopy(legacy.get("playlist", {}))
            for item in prepared.get("items", []):
                url, _runtime = portable_playlist_url(str(item.get("url") or ""), self.folder)
                item["url"] = url
                item.setdefault("auto_title", bool(create_playlist_item(
                    title=str(item.get("title") or ""), url=url,
                    type=str(item.get("type") or "image"),
                )["auto_title"]))
            seed = self.entities(prepared)
            seed["$migration:playlist"] = {AUXILIARY: True, "version": 1}
            seed.update({
                _SOURCES + name: {"entry": deepcopy(entry), AUXILIARY: True, "source_name": name}
                for name, entry in legacy.get("processed", {}).items()
            })
            # Keep the original bytes, not a reserialized/machine-resolved copy.
            if original_bytes is not None:
                backup = self.replica.state_dir / "migration" / (hashlib.sha256(original_bytes).hexdigest() + ".json")
                if not backup.exists():
                    write_bytes_atomic(backup, original_bytes)
        migrated = self.replica.read(seed) if seed is not None else current
        self._migration_verified = seed is not None
        return migrated

    def manifest(self, snapshot: ReplicaSnapshot) -> dict:
        projected = reconcile_discoveries(snapshot.entities)
        return {
            "version": 2,
            "processed": {
                str(entity["source_name"]): deepcopy(entity["entry"])
                for entity in snapshot.entities.values() if entity.get("source_name")
            },
            "playlist": _playlist(projected),
        }

    def discover(self, snapshot: ReplicaSnapshot, items: list[dict]) -> ReplicaSnapshot:
        desired = deepcopy(snapshot.entities)
        blocked = suppressed_resources(desired)
        deleted_occurrences = {str(entity.get("occurrence_id") or "")
                               for entity in desired.values() if entity.get(SUPPRESSED)}
        claims = {resource_identity_key(str(node.get(RESOURCE)))
                  for node in desired.values() if node.get(RESOURCE)}
        additions = []
        for item in items:
            candidate = deepcopy(item)
            resource = portable_resource_key(str(candidate.get("url") or ""), self.folder)
            resource_identity = resource_identity_key(resource)
            if not resource:
                continue
            candidate[RESOURCE] = resource
            candidate["url"] = resource
            if not candidate.get("_virtual"):
                if resource_identity in claims or resource_identity in blocked:
                    continue
                source = str(candidate.get("_source") or "")
                output_index = candidate.get("_source_output")
                candidate["id"] = (
                    str(uuid.uuid5(uuid.NAMESPACE_URL,
                        f"solin:output:{resource_identity_key(source)}:{output_index}"))
                    if source and isinstance(output_index, int)
                    else automatic_occurrence_id(resource)
                )
                if candidate["id"] in deleted_occurrences:
                    continue
                candidate[DISCOVERED] = True
                claims.add(resource_identity)
            elif str(candidate.get("id") or "") in desired.keys() | deleted_occurrences:
                continue
            additions.append(candidate)
        if not additions:
            return snapshot
        view = _playlist(reconcile_discoveries(desired))
        view["items"].extend(additions)
        for key, value in self.entities(view, desired).items():
            if key not in desired:
                desired[key] = value
        return self.replica.commit(snapshot, desired)

    def save(self, playlist: dict, *, stage: bool = False) -> ReplicaSnapshot:
        if not stage:
            self.replica.initialize_document()
        value = playlist.get(STATE)
        base = ReplicaSnapshot.from_dict(value) if isinstance(value, dict) else self.read()
        desired = self.entities(playlist, reconcile_discoveries(base.entities))
        desired = record_discovery_edits(base.entities, desired)
        staged = self.replica.stage(base, desired)
        # The caller still holds its original view. Advancing that view's base
        # to include unseen remote inserts turns a retry into their deletion.
        playlist[STATE] = staged.to_dict()
        return staged if stage else self.replica.commit(staged, staged.entities)

    def reset_document(self) -> None:
        try:
            previous = self.read()
        except JournalError:
            # A conflicting remote identity cannot choose this terminal's
            # reset baseline. Preserve its last validated durable local view.
            previous = self.replica.read_local()
        preserved = deepcopy(previous.entities)
        for node in list(preserved.values()):
            if node.get(RESOURCE) and previous.document_id:
                resource = resource_identity_key(str(node[RESOURCE]))
                key = "$archive:" + hashlib.sha256(resource.encode()).hexdigest()
                documents = preserved.get(key, {}).get("documents", [])
                preserved[key] = {AUXILIARY: True, "archive_resource": resource,
                    "documents": list(dict.fromkeys([previous.document_id, *documents]))}
        self.replica.reset_document()
        base = self.replica.read()
        self.replica.commit(base, preserved)
        self._migration_verified = True

    def rebind_folder(self, folder: Path) -> None:
        old_folder = self.folder
        intents = {
            self.replica.state_dir / "intents" / f"{intent[INTENT]['id']}.json": intent
            for intent in self.pending_intents()
        }
        originals = {path: path.read_bytes() for path in intents}
        rewritten: list[Path] = []
        try:
            with self.replica.rebinding_folder(folder):
                self.folder = folder
                try:
                    for path, intent in intents.items():
                        self.rebase_urls(intent, old_folder)
                        # An atomic replacement can succeed before durability
                        # confirmation fails; roll back that attempted file too.
                        rewritten.append(path)
                        JsonFileRepository(path).write(intent)
                except BaseException:  # noqa: BLE001 - Roll back pending intent bytes, then re-raise.
                    for path in rewritten:
                        write_bytes_atomic(path, originals[path])
                    raise
        except BaseException:  # noqa: BLE001 - Restore adapter binding on every failed transaction.
            self.folder = old_folder
            raise

    def rebase_urls(self, playlist: dict, old_folder: Path) -> None:
        for item in playlist.get("items", []):
            for field in ("url", "source_url", "thumbnail_url"):
                value = str(item.get(field) or "")
                try:
                    relative = Path(value).relative_to(old_folder)
                except ValueError:
                    continue
                item[field] = str(self.folder / relative)

    def update_processed(self, snapshot: ReplicaSnapshot, source: str, entry: dict) -> ReplicaSnapshot:
        desired = deepcopy(snapshot.entities)
        desired[_SOURCES + source] = {"entry": deepcopy(entry), AUXILIARY: True, "source_name": source}
        previous = snapshot.entities.get(_SOURCES + source, {}).get("entry", {})
        old_outputs = [resource_identity_key(".solin_cache/" + name)
                       for name in previous.get("outputs", [])]
        new_outputs = [".solin_cache/" + name for name in entry.get("outputs", [])]
        replacements = dict(zip(old_outputs, new_outputs, strict=False))
        removed_outputs = set(old_outputs[len(new_outputs):])
        old_virtuals = {str(item.get("id")) for item in previous.get("virtual_items", [])}
        new_virtuals = {str(item.get("id")) for item in entry.get("virtual_items", [])}
        for node_id, node in list(desired.items()):
            resource = str(node.get(RESOURCE) or node.get("url") or "")
            key = resource_identity_key(resource)
            if node.get("type") != "media":
                continue
            if (key in replacements and not node.get("_virtual")
                    and node.get("_source", source) == source):
                node["url"] = replacements[key]
                node[RESOURCE] = node["url"]
            obsolete_page = key in removed_outputs and node.get(DISCOVERED)
            obsolete_virtual = node_id in old_virtuals - new_virtuals
            if (obsolete_page or obsolete_virtual) and node.get("_source") == source:
                desired.pop(node_id)
                deletion_id, deletion = suppression_record(node_id, resource,
                    automatic=bool(node.get(DISCOVERED)))
                desired[deletion_id] = deletion
        for virtual in entry.get("virtual_items", []):
            node = desired.get(str(virtual.get("id") or ""))
            if node is not None:
                url = str(virtual.get("url") or "")
                if url:
                    node["url"] = portable_resource_key(url, self.folder)
                    node[RESOURCE] = node["url"]
        return self.replica.commit(snapshot, desired)

    def reconcile_resources(self, snapshot: ReplicaSnapshot) -> None:
        self.resource_errors = []
        visible = reconcile_discoveries(snapshot.entities)
        references = {resource_identity_key(str(node[RESOURCE])): str(node[RESOURCE])
                      for node in visible.values() if node.get(RESOURCE) and not node.get(AUXILIARY)}
        archive_documents = {node["archive_resource"]: node["documents"]
                             for node in visible.values() if node.get("archive_resource")}
        suppressed = {resource_identity_key(str(node[SUPPRESSED])): str(node[SUPPRESSED])
                      for node in snapshot.entities.values() if node.get(SUPPRESSED)}
        for identity, resource in (suppressed | references).items():
            if resource.startswith(("http://", "https://")):
                continue
            try:
                if identity in references:
                    recovered = recover_file(self.folder, resource, document_id=snapshot.document_id)
                    for origin in archive_documents.get(identity, []):
                        if recovered:
                            break
                        recovered = recover_file(self.folder, resource, document_id=origin)
                else:
                    retire_file(self.folder, resource, document_id=snapshot.document_id)
            except OSError as exc:
                self.resource_errors.append(str(exc))
                log.warning("Linked resource cleanup/recovery remains pending: %s", resource,
                            exc_info=True)

    def stage_intent(self, playlist: dict) -> None:
        """Persist UI edits locally before the cloud/copy worker starts."""
        previous = playlist.get(INTENT, {})
        key = str(previous.get("id") or uuid.uuid4().hex)
        generation = int(previous.get("generation", 0)) + 1
        playlist[INTENT] = {"id": key, "generation": generation}
        repository = JsonFileRepository(self.replica.state_dir / "intents" / f"{key}.json")
        # Preserve the raw local source before the operation can be published
        # by a concurrent worker or validation can fail. Replay is idempotent.
        repository.write(playlist)
        value = playlist.get(STATE)
        if isinstance(value, dict):
            base = ReplicaSnapshot.from_dict(value)
            desired = self.entities(playlist, reconcile_discoveries(base.entities),
                                    local_intent=True)
            desired = record_discovery_edits(base.entities, desired)
            playlist[STATE] = self.replica.stage(base, desired).to_dict()
            repository.write(playlist)

    def pending_intents(self) -> list[dict]:
        with self.intent_lock:
            directory = self.replica.state_dir / "intents"
            if not directory.exists():
                return []
            return [JsonFileRepository(path).read() for path in sorted(directory.glob("*.json"))]

    def acknowledge_intent(self, playlist: dict) -> None:
        intent = playlist.get(INTENT)
        if not isinstance(intent, dict):
            return
        key = str(intent.get("id") or "")
        if not key or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Invalid local intent ID")
        path = self.replica.state_dir / "intents" / f"{key}.json"
        with self.intent_lock:
            if path.exists() and JsonFileRepository(path).read().get(INTENT) == intent:
                path.unlink()


def read_linked_manifest(folder: Path) -> dict:
    """Read the journal projection, importing the frozen legacy baseline once."""
    service = playlist_sync(folder)
    with service.lock:
        snapshot = service.read()
        return service.manifest(snapshot)
