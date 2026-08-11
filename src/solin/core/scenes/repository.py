from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from solin.core.scenes.collections import SceneCollectionCatalog
from solin.core.scenes.model import (
    DEFAULT_CAMERA_SOURCE_ID,
    SceneDocument,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    UnsupportedSceneSchemaError,
)
from solin.core.scenes.resources import SceneResourceCatalog
from solin.core.scenes.runtime import (
    SceneRuntimeState,
    create_default_runtime_state,
)
from solin.core.storage.json_repository import JsonFileRepository

MAX_SCENE_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_SCENE_COLLECTION_CATALOG_BYTES = 128 * 1024
MAX_SCENE_RESOURCE_CATALOG_BYTES = 2 * 1024 * 1024
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}
log = logging.getLogger(__name__)


class SceneRepositoryError(RuntimeError):
    pass


class SceneRepositoryCorruptError(SceneRepositoryError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"Scene document is unreadable or invalid: {path}")
        self.path = path


class SceneRevisionConflictError(SceneRepositoryError):
    pass


class _DuplicateJsonKeyError(ValueError):
    pass


class SceneDocumentRepository:
    """Atomic scene storage with validation, backup, and optimistic revisions."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        seed_factory: Callable[[], SceneDocument],
        backup_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._primary = JsonFileRepository(path)
        default_backup = self._primary.path.with_name(
            f"{self._primary.path.stem}.backup{self._primary.path.suffix}"
        )
        self._backup = JsonFileRepository(backup_path or default_backup)
        if self._primary.path.resolve() == self._backup.path.resolve():
            raise ValueError("Scene primary and backup paths must be different")
        self._seed_factory = seed_factory
        self._lock_path = self._primary.path.with_suffix(f"{self._primary.path.suffix}.lock")
        resolved_path = self._primary.path.resolve()
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(resolved_path, threading.RLock())

    @property
    def path(self) -> Path:
        return self._primary.path

    @property
    def backup_path(self) -> Path:
        return self._backup.path

    def load(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._primary.exists():
                raise FileNotFoundError(self._primary.path)
            return self._load_repository(self._primary)

    def load_or_create(self) -> SceneDocument:
        with self._transaction_lock():
            if self._primary.exists():
                return self._load_repository(self._primary)
            document = self._seed_factory()
            self._write(self._primary, document)
            return document

    def load_backup(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._backup.exists():
                raise FileNotFoundError(self._backup.path)
            return self._load_repository(self._backup)

    def initialize_backup(self, document: SceneDocument) -> None:
        """Create the first known-good recovery point without replacing one."""

        with self._transaction_lock():
            if not self._backup.exists():
                self._write(self._backup, document)

    def save(
        self,
        document: SceneDocument,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._transaction_lock():
            current = self._load_repository(self._primary) if self._primary.exists() else None
            self._validate_save(
                document,
                current=current,
                expected_revision=expected_revision,
            )
            if current == document:
                return
            if current is not None:
                recovery_revision = document.revision + 1
                self._write(
                    self._backup,
                    replace(current, revision=recovery_revision),
                )
            self._write(self._primary, document)

    def recover_from_backup(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._backup.exists():
                raise FileNotFoundError(self._backup.path)
            document = self._load_repository(self._backup)
            if self._primary.exists():
                try:
                    current = self._load_repository(self._primary)
                except SceneRepositoryCorruptError:
                    current = None
                if current is not None and document.revision <= current.revision:
                    document = replace(document, revision=current.revision + 1)
            self._write(self._primary, document)
            return document

    @staticmethod
    def _load_repository(repository: JsonFileRepository) -> SceneDocument:
        try:
            record = _read_json_record(
                repository.path,
                maximum_bytes=MAX_SCENE_DOCUMENT_BYTES,
            )
            return SceneDocument.from_record(record)
        except UnsupportedSceneSchemaError:
            raise
        except (
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise SceneRepositoryCorruptError(repository.path) from exc

    @staticmethod
    def _write(repository: JsonFileRepository, document: SceneDocument) -> None:
        repository.write(
            document.to_record(),
            indent=2,
            sort_keys=False,
            trailing_newline=True,
        )

    @staticmethod
    def _validate_save(
        document: SceneDocument,
        *,
        current: SceneDocument | None,
        expected_revision: int | None,
    ) -> None:
        if current is None:
            if expected_revision not in {None, 0}:
                raise SceneRevisionConflictError(
                    "Scene document does not exist at the expected revision"
                )
            return
        if document.document_id != current.document_id:
            raise SceneRevisionConflictError("Scene document identity changed")
        if expected_revision is not None and current.revision != expected_revision:
            raise SceneRevisionConflictError(
                f"Expected scene revision {expected_revision}, found {current.revision}"
            )
        if document != current and document.revision <= current.revision:
            raise SceneRevisionConflictError("A scene update must have a newer document revision")

    @contextmanager
    def _transaction_lock(self):
        with self._lock:
            with _interprocess_lock(self._lock_path):
                yield


class SceneCollectionDocumentRepository:
    """Persist collection-local documents with camera references materialized on load."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        resources: Callable[[], SceneResourceCatalog],
        backup_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._primary = JsonFileRepository(path)
        default_backup = self._primary.path.with_name(
            f"{self._primary.path.stem}.backup{self._primary.path.suffix}"
        )
        self._backup = JsonFileRepository(backup_path or default_backup)
        self._resources = resources
        self._lock_path = self._primary.path.with_suffix(f"{self._primary.path.suffix}.lock")
        resolved_path = self._primary.path.resolve()
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(resolved_path, threading.RLock())

    @property
    def path(self) -> Path:
        return self._primary.path

    @property
    def backup_path(self) -> Path:
        return self._backup.path

    def load(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._primary.exists():
                raise FileNotFoundError(self._primary.path)
            return self._load_repository(self._primary)

    def load_backup(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._backup.exists():
                raise FileNotFoundError(self._backup.path)
            return self._load_repository(self._backup)

    def initialize_backup(self, document: SceneDocument) -> None:
        """Create the first known-good recovery point without replacing one."""

        with self._transaction_lock():
            if not self._backup.exists():
                self._backup.write(
                    self._to_storage_record(document),
                    indent=2,
                    sort_keys=False,
                    trailing_newline=True,
                )

    def save(
        self,
        document: SceneDocument,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._transaction_lock():
            current = self._load_repository(self._primary) if self._primary.exists() else None
            SceneDocumentRepository._validate_save(
                document,
                current=current,
                expected_revision=expected_revision,
            )
            candidate_record = self._to_storage_record(document)
            current_record = (
                _read_json_record(
                    self._primary.path,
                    maximum_bytes=MAX_SCENE_DOCUMENT_BYTES,
                )
                if self._primary.exists()
                else None
            )
            if current_record == candidate_record:
                return
            if current is not None:
                recovery = replace(current, revision=document.revision + 1)
                self._backup.write(
                    self._to_storage_record(recovery),
                    indent=2,
                    sort_keys=False,
                    trailing_newline=True,
                )
            self._primary.write(
                candidate_record,
                indent=2,
                sort_keys=False,
                trailing_newline=True,
            )

    def normalize_storage(self) -> None:
        """Rewrite legacy full-camera records into reference form without a revision bump."""

        with self._transaction_lock():
            if not self._primary.exists():
                raise FileNotFoundError(self._primary.path)
            self._normalize_repository(self._primary)
            if self._backup.exists():
                try:
                    self._normalize_repository(self._backup)
                except SceneRepositoryCorruptError:
                    current = self._load_repository(self._primary)
                    self._backup.write(
                        self._to_storage_record(current),
                        indent=2,
                        sort_keys=False,
                        trailing_newline=True,
                    )

    def recover_from_backup(self) -> SceneDocument:
        with self._transaction_lock():
            if not self._backup.exists():
                raise FileNotFoundError(self._backup.path)
            document = self._load_repository(self._backup)
            current: SceneDocument | None = None
            if self._primary.exists():
                try:
                    current = self._load_repository(self._primary)
                except SceneRepositoryCorruptError:
                    pass
            if current is not None and document.revision <= current.revision:
                document = replace(document, revision=current.revision + 1)
            self._primary.write(
                self._to_storage_record(document),
                indent=2,
                sort_keys=False,
                trailing_newline=True,
            )
            return document

    def _normalize_repository(self, repository: JsonFileRepository) -> None:
        document = self._load_repository(repository)
        record = self._to_storage_record(document)
        current = _read_json_record(
            repository.path,
            maximum_bytes=MAX_SCENE_DOCUMENT_BYTES,
        )
        if current != record:
            repository.write(
                record,
                indent=2,
                sort_keys=False,
                trailing_newline=True,
            )

    def _load_repository(self, repository: JsonFileRepository) -> SceneDocument:
        try:
            record = _read_json_record(
                repository.path,
                maximum_bytes=MAX_SCENE_DOCUMENT_BYTES,
            )
            return SceneDocument.from_record(
                self._materialize_storage_record(record)
            )
        except UnsupportedSceneSchemaError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise SceneRepositoryCorruptError(repository.path) from exc

    def _materialize_storage_record(self, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise SceneValidationError("Scene collection document must be an object")
        record = json.loads(json.dumps(raw))
        raw_sources = record.get("sources")
        if not isinstance(raw_sources, list):
            raise SceneValidationError("Scene collection sources must be an array")
        resources = self._resources()
        camera_by_id = {camera.id: camera for camera in resources.cameras}
        materialized_sources: list[object] = []
        active_camera_ids: set[str] = set()
        source_aliases: dict[str, str] = {}
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                raise SceneValidationError("Scene collection source must be an object")
            source_type = raw_source.get("type")
            source_id = raw_source.get("id")
            if source_type == "camera_ref":
                if set(raw_source) != {"id", "type", "enabled"}:
                    raise SceneValidationError("Invalid shared camera reference")
                if not isinstance(source_id, str) or not isinstance(
                    raw_source.get("enabled"), bool
                ):
                    raise SceneValidationError("Invalid shared camera reference")
                camera = camera_by_id.get(source_id)
                if (
                    camera is None
                    and source_id == DEFAULT_CAMERA_SOURCE_ID
                    and resources.cameras
                ):
                    camera = resources.cameras[0]
                    source_aliases[source_id] = camera.id
                if camera is None:
                    raise SceneValidationError("Shared camera reference is missing")
                materialized_sources.append(
                    replace(camera, enabled=raw_source["enabled"]).to_record()
                )
                active_camera_ids.add(camera.id)
                continue
            if source_type in {
                SourceKind.LOCAL_CAMERA.value,
                SourceKind.RTSP_CAMERA.value,
            }:
                if not isinstance(source_id, str):
                    raise SceneValidationError("Invalid camera source id")
                camera = camera_by_id.get(source_id)
                if camera is not None:
                    enabled = raw_source.get("enabled", True)
                    if not isinstance(enabled, bool):
                        raise SceneValidationError("Invalid camera source state")
                    materialized_sources.append(replace(camera, enabled=enabled).to_record())
                    active_camera_ids.add(source_id)
                    continue
            materialized_sources.append(raw_source)
        record["sources"] = materialized_sources
        if source_aliases:
            self._retarget_layer_sources(record, source_aliases)
        record["camera_presets"] = [
            preset.to_record()
            for preset in resources.camera_presets
            if preset.camera_source_id in active_camera_ids
        ]
        return record

    @staticmethod
    def _retarget_layer_sources(
        record: dict[str, Any],
        aliases: dict[str, str],
    ) -> None:
        scenes = record.get("scenes")
        if not isinstance(scenes, list):
            return
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            layers = scene.get("layers")
            if not isinstance(layers, list):
                continue
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                source_id = layer.get("source_id")
                if isinstance(source_id, str) and source_id in aliases:
                    layer["source_id"] = aliases[source_id]

    @staticmethod
    def _camera_reference_record(source: SourceDefinition) -> dict[str, object]:
        return {
            "id": source.id,
            "type": "camera_ref",
            "enabled": source.enabled,
        }

    def _to_storage_record(self, document: SceneDocument) -> dict[str, object]:
        record = document.to_record()
        record["sources"] = [
            (
                self._camera_reference_record(source)
                if source.kind in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}
                else source.to_record()
            )
            for source in document.sources
        ]
        record["camera_presets"] = []
        return record

    @contextmanager
    def _transaction_lock(self):
        with self._lock:
            with _interprocess_lock(self._lock_path):
                yield


class SceneCollectionCatalogRepository:
    """Atomic, optimistic store for the ordered Scene-profile catalog."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._repository = JsonFileRepository(path)
        self._lock_path = self._repository.path.with_suffix(
            f"{self._repository.path.suffix}.lock"
        )
        resolved_path = self._repository.path.resolve()
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(resolved_path, threading.RLock())

    @property
    def path(self) -> Path:
        return self._repository.path

    def exists(self) -> bool:
        return self._repository.exists()

    def load(self) -> SceneCollectionCatalog:
        with self._transaction_lock():
            if not self._repository.exists():
                raise FileNotFoundError(self._repository.path)
            return self._load()

    def save(
        self,
        catalog: SceneCollectionCatalog,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._transaction_lock():
            current = self._load() if self._repository.exists() else None
            if current is None:
                if expected_revision not in {None, 0}:
                    raise SceneRevisionConflictError(
                        "Scene profile catalog does not exist at the expected revision"
                    )
            else:
                if expected_revision is not None and current.revision != expected_revision:
                    raise SceneRevisionConflictError(
                        f"Expected Scene profile catalog revision {expected_revision}, "
                        f"found {current.revision}"
                    )
                if catalog != current and catalog.revision <= current.revision:
                    raise SceneRevisionConflictError(
                        "A Scene profile catalog update must have a newer revision"
                    )
            if catalog != current:
                self._repository.write(
                    catalog.to_record(),
                    indent=2,
                    sort_keys=False,
                    trailing_newline=True,
                )

    def _load(self) -> SceneCollectionCatalog:
        try:
            record = _read_json_record(
                self._repository.path,
                maximum_bytes=MAX_SCENE_COLLECTION_CATALOG_BYTES,
            )
            return SceneCollectionCatalog.from_record(record)
        except (OSError, TypeError, ValueError) as exc:
            raise SceneRepositoryCorruptError(self._repository.path) from exc

    @contextmanager
    def _transaction_lock(self):
        with self._lock:
            with _interprocess_lock(self._lock_path):
                yield


class SceneResourceCatalogRepository:
    """Atomic, optimistic store for cameras and PTZ shared by Scene profiles."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._repository = JsonFileRepository(path)
        self._lock_path = self._repository.path.with_suffix(
            f"{self._repository.path.suffix}.lock"
        )
        resolved_path = self._repository.path.resolve()
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(resolved_path, threading.RLock())

    def exists(self) -> bool:
        return self._repository.exists()

    def load(self) -> SceneResourceCatalog:
        with self._transaction_lock():
            if not self._repository.exists():
                raise FileNotFoundError(self._repository.path)
            return self._load()

    def save(
        self,
        resources: SceneResourceCatalog,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._transaction_lock():
            current = self._load() if self._repository.exists() else None
            if current is None:
                if expected_revision not in {None, 0}:
                    raise SceneRevisionConflictError(
                        "Shared scene resources do not exist at the expected revision"
                    )
            else:
                if expected_revision is not None and current.revision != expected_revision:
                    raise SceneRevisionConflictError(
                        f"Expected shared scene-resource revision {expected_revision}, "
                        f"found {current.revision}"
                    )
                if resources != current and resources.revision <= current.revision:
                    raise SceneRevisionConflictError(
                        "A shared scene-resource update must have a newer revision"
                    )
            if resources != current:
                self._repository.write(
                    resources.to_record(),
                    indent=2,
                    sort_keys=False,
                    trailing_newline=True,
                )

    def _load(self) -> SceneResourceCatalog:
        try:
            record = _read_json_record(
                self._repository.path,
                maximum_bytes=MAX_SCENE_RESOURCE_CATALOG_BYTES,
            )
            return SceneResourceCatalog.from_record(record)
        except (OSError, TypeError, ValueError) as exc:
            raise SceneRepositoryCorruptError(self._repository.path) from exc

    @contextmanager
    def _transaction_lock(self):
        with self._lock:
            with _interprocess_lock(self._lock_path):
                yield


class SceneRuntimeRepository:
    """Small atomic store for live output state, separate from scene history."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._repository = JsonFileRepository(path)
        self._lock_path = self._repository.path.with_suffix(f"{self._repository.path.suffix}.lock")
        resolved_path = self._repository.path.resolve()
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(resolved_path, threading.RLock())

    @property
    def path(self) -> Path:
        return self._repository.path

    def load_or_create(self, document: SceneDocument) -> SceneRuntimeState:
        with self._transaction_lock():
            if self._repository.exists():
                state = self._load()
                if state.document_id == document.document_id:
                    reconciled = state.reconciled_against(document)
                    reconciled.validate_against(document)
                    if reconciled != state:
                        self._write(reconciled)
                    return reconciled
            state = create_default_runtime_state(document)
            self._write(state)
            return state

    def save(
        self,
        state: SceneRuntimeState,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._transaction_lock():
            current = self._load() if self._repository.exists() else None
            if current is None:
                if expected_revision not in {None, 0}:
                    raise SceneRevisionConflictError(
                        "Scene runtime state does not exist at the expected revision"
                    )
            else:
                if state.document_id != current.document_id:
                    raise SceneRevisionConflictError("Scene runtime identity changed")
                if expected_revision is not None and current.revision != expected_revision:
                    raise SceneRevisionConflictError(
                        f"Expected runtime revision {expected_revision}, found {current.revision}"
                    )
                if state != current and state.revision <= current.revision:
                    raise SceneRevisionConflictError("A runtime update must have a newer revision")
            if state != current:
                self._write(state)

    def _load(self) -> SceneRuntimeState:
        try:
            record = _read_json_record(self._repository.path, maximum_bytes=64 * 1024)
            return SceneRuntimeState.from_record(record)
        except (OSError, TypeError, ValueError) as exc:
            raise SceneRepositoryCorruptError(self._repository.path) from exc

    def _write(self, state: SceneRuntimeState) -> None:
        self._repository.write(
            state.to_record(),
            indent=2,
            sort_keys=False,
            trailing_newline=True,
        )

    @contextmanager
    def _transaction_lock(self):
        with self._lock:
            with _interprocess_lock(self._lock_path):
                yield


class SceneRuntimePersistenceQueue:
    """Serialize and coalesce live-state writes away from the caller thread."""

    def __init__(
        self,
        repository: SceneRuntimeRepository,
        initial_state: SceneRuntimeState,
        *,
        retry_delay_seconds: float = 0.05,
    ) -> None:
        if retry_delay_seconds <= 0:
            raise ValueError("Runtime persistence retry delay must be positive")
        self._repository = repository
        self._document_id = initial_state.document_id
        self._accepted_state = initial_state
        self._persisted_revision = initial_state.revision
        self._pending: SceneRuntimeState | None = None
        self._inflight = False
        self._closing = False
        self._closed = False
        self._last_error: Exception | None = None
        self._retry_delay_seconds = retry_delay_seconds
        self._condition = threading.Condition()
        self._thread = threading.Thread(
            target=self._run,
            name="solin-scene-runtime-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def last_error(self) -> Exception | None:
        with self._condition:
            return self._last_error

    def save(
        self,
        state: SceneRuntimeState,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._condition:
            if self._closing or self._closed:
                raise SceneRepositoryError("Scene runtime persistence is closed")
            if state.document_id != self._document_id:
                raise SceneRevisionConflictError("Scene runtime identity changed")
            accepted_revision = self._accepted_state.revision
            if expected_revision is not None and expected_revision != accepted_revision:
                raise SceneRevisionConflictError(
                    f"Expected queued runtime revision {expected_revision}, "
                    f"found {accepted_revision}"
                )
            if state == self._accepted_state:
                return
            if state.revision <= accepted_revision:
                raise SceneRevisionConflictError(
                    "A queued runtime update must have a newer revision"
                )
            self._accepted_state = state
            self._pending = state
            self._condition.notify_all()

    def flush(self, timeout_seconds: float = 2.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("Runtime persistence timeout cannot be negative")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while (
                self._persisted_revision < self._accepted_state.revision
                or self._pending is not None
                or self._inflight
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout_seconds: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        flushed = self.flush(max(0.0, deadline - time.monotonic()))
        with self._condition:
            self._closing = True
            if not flushed:
                self._pending = None
            self._condition.notify_all()
        self._thread.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            self._closed = not self._thread.is_alive()
        if not flushed:
            log.warning("Timed out while flushing scene runtime persistence")
        if not self._closed:
            log.warning("Scene runtime persistence worker did not stop within its deadline")
        return flushed and self._closed

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closing:
                    self._condition.wait()
                if self._closing:
                    self._closed = True
                    self._condition.notify_all()
                    return
                state = self._pending
                self._pending = None
                self._inflight = True
                expected_revision = self._persisted_revision
            assert state is not None
            try:
                self._repository.save(
                    state,
                    expected_revision=expected_revision,
                )
            except Exception as exc:  # noqa: BLE001 - durable storage boundary
                with self._condition:
                    self._inflight = False
                    self._last_error = exc
                    if (
                        not self._closing
                        and (
                            self._pending is None
                            or self._pending.revision < state.revision
                        )
                    ):
                        self._pending = state
                    self._condition.notify_all()
                    if not self._closing:
                        self._condition.wait(self._retry_delay_seconds)
                log.warning("Could not persist scene runtime state; retrying", exc_info=exc)
                continue
            with self._condition:
                self._persisted_revision = state.revision
                self._inflight = False
                self._last_error = None
                self._condition.notify_all()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_record(path: Path, *, maximum_bytes: int) -> Any:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ValueError("JSON document exceeds the size limit")
    return json.loads(
        path.read_bytes().decode("utf-8"),
        object_pairs_hook=_unique_json_object,
    )


@contextmanager
def _interprocess_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
