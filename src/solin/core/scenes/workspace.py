from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.collections import (
    DEFAULT_SCENE_COLLECTION_ID,
    MAX_SCENE_COLLECTIONS,
    SceneCollection,
    SceneCollectionCatalog,
    validate_scene_collection_name,
)
from solin.core.scenes.model import (
    BusId,
    CameraPreset,
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    OutputMode,
    RtspCameraConfig,
    SceneDocument,
    SourceDefinition,
    SourceKind,
    new_identity,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_fresh_scene_collection_document,
    ensure_default_scene,
)
from solin.core.scenes.repository import (
    SceneCollectionCatalogRepository,
    SceneCollectionDocumentRepository,
    SceneDocumentRepository,
    SceneRepositoryCorruptError,
    SceneResourceCatalogRepository,
    SceneRuntimeRepository,
    SceneRuntimePersistenceQueue,
)
from solin.core.scenes.resources import (
    SceneResourceCatalog,
    cameras_from_document_sources,
)
from solin.core.scenes.recording import SceneRecordingConfig
from solin.core.scenes.runtime import (
    OutputRuntimeState,
    SceneRuntimeService,
    SceneRuntimeState,
    create_default_runtime_state,
)


log = logging.getLogger(__name__)


class SceneWorkspaceOperationError(RuntimeError):
    pass


class SceneWorkspaceBusyError(SceneWorkspaceOperationError):
    pass


class SceneWorkspaceChangeKind(StrEnum):
    CATALOG = "catalog"
    ACTIVATED = "activated"
    RESOURCES = "resources"


@dataclass(frozen=True, slots=True)
class SceneWorkspaceChange:
    kind: SceneWorkspaceChangeKind
    catalog: SceneCollectionCatalog
    collection: SceneCollection
    document: SceneDocument


@dataclass(frozen=True, slots=True)
class SceneWorkspaceActivation:
    source_collection_id: str
    target: SceneCollection
    documents: SceneDocumentService
    runtime: SceneRuntimeService
    catalog_revision: int


class SceneWorkspaceService:
    """Own the active Scene profile and its replaceable document/runtime services."""

    def __init__(
        self,
        profile_paths: ProfilePaths,
        *,
        seed_names: SceneSeedNames,
        identity_factory: Callable[[], str] = new_identity,
    ) -> None:
        self._paths = profile_paths
        self._seed_names = seed_names
        self._identity_factory = identity_factory
        self._catalog_repository = SceneCollectionCatalogRepository(
            profile_paths.scene_profiles_catalog_file
        )
        self._runtime_repository = SceneRuntimeRepository(profile_paths.scenes_runtime_file)
        self._resource_repository = SceneResourceCatalogRepository(
            profile_paths.scene_resources_file
        )
        self._credential_cleaner: Callable[[str], None] | None = None
        self._listeners: set[Callable[[SceneWorkspaceChange], None]] = set()
        self._catalog_mutation_guards: set[Callable[[str], str]] = set()
        self._catalog = self._load_or_migrate_catalog()
        self._resources = self._load_or_migrate_resources()
        self._normalize_collection_storage()
        self._documents, self._runtime, self._runtime_persistence = self._load_services(
            self._catalog.active_collection_id,
            previous_runtime=None,
        )
        self._unsubscribe_documents = self._documents.subscribe(
            self._on_active_document_changed
        )

    @property
    def catalog(self) -> SceneCollectionCatalog:
        return self._catalog

    @property
    def active_collection(self) -> SceneCollection:
        return self._catalog.active

    @property
    def documents(self) -> SceneDocumentService:
        return self._documents

    @property
    def runtime(self) -> SceneRuntimeService:
        return self._runtime

    @property
    def resources(self) -> SceneResourceCatalog:
        return self._resources

    @property
    def configured_cameras(self) -> tuple[SourceDefinition, ...]:
        return self._resources.cameras

    @property
    def virtual_camera_enabled(self) -> bool:
        return self._runtime.state.output(BusId.VIRTUAL_CAMERA).enabled

    def subscribe(
        self,
        listener: Callable[[SceneWorkspaceChange], None],
    ) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def set_credential_cleaner(self, cleaner: Callable[[str], None]) -> None:
        """Attach the profile-scoped vault and retry durable credential cleanup."""

        self._credential_cleaner = cleaner
        self._flush_pending_credential_deletions()

    def register_catalog_mutation_guard(
        self,
        guard: Callable[[str], str],
    ) -> Callable[[], None]:
        """Register a domain-level blocker for Scene-profile lifecycle changes."""

        self._catalog_mutation_guards.add(guard)

        def unsubscribe() -> None:
            self._catalog_mutation_guards.discard(guard)

        return unsubscribe

    def update_active_recording_config(
        self,
        recording: SceneRecordingConfig,
    ) -> SceneCollection:
        if not isinstance(recording, SceneRecordingConfig):
            raise TypeError("Invalid Scene recording configuration")
        current = self._catalog.active
        if current.recording == recording:
            return current
        updated_collection = replace(current, recording=recording)
        updated_catalog = replace(
            self._catalog,
            revision=self._catalog.revision + 1,
            collections=tuple(
                updated_collection if item.id == current.id else item
                for item in self._catalog.collections
            ),
        )
        self._save_catalog(updated_catalog)
        self._publish(SceneWorkspaceChangeKind.CATALOG)
        return updated_collection

    def create_collection(self, name: str) -> SceneCollection:
        self._require_mutable_catalog("create")
        if len(self._catalog.collections) >= MAX_SCENE_COLLECTIONS:
            raise SceneWorkspaceOperationError("The Scene profile limit has been reached")
        canonical_name = validate_scene_collection_name(
            name,
            existing=self._catalog.collections,
        )
        collection = SceneCollection(id=self._identity_factory(), name=canonical_name)
        document = create_fresh_scene_collection_document(
            self._seed_names,
            document_id=self._identity_factory(),
        )
        repository = self._document_repository(collection.id)
        try:
            repository.save(document)
            repository.initialize_backup(document)
            updated = replace(
                self._catalog,
                revision=self._catalog.revision + 1,
                collections=(*self._catalog.collections, collection),
            )
            self._save_catalog(updated)
        except Exception:  # noqa: BLE001 - roll back partially created profile files
            self._remove_collection_files(collection.id)
            raise
        self._publish(SceneWorkspaceChangeKind.CATALOG)
        return collection

    def rename_collection(self, collection_id: str, name: str) -> SceneCollection:
        current = self._catalog.collection(collection_id)
        canonical_name = validate_scene_collection_name(
            name,
            existing=self._catalog.collections,
            excluding_id=collection_id,
        )
        if canonical_name == current.name:
            return current
        renamed = replace(current, name=canonical_name)
        updated = replace(
            self._catalog,
            revision=self._catalog.revision + 1,
            collections=tuple(
                renamed if item.id == collection_id else item
                for item in self._catalog.collections
            ),
        )
        self._save_catalog(updated)
        self._publish(SceneWorkspaceChangeKind.CATALOG)
        return renamed

    def upsert_camera(
        self,
        camera: SourceDefinition,
        *,
        add_to_active_document: bool = False,
    ) -> SourceDefinition:
        if camera.kind not in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}:
            raise SceneWorkspaceOperationError("Shared scene resources accept only cameras")
        existing = next(
            (candidate for candidate in self._resources.cameras if candidate.id == camera.id),
            None,
        )
        cameras = (
            tuple(
                camera if candidate.id == camera.id else candidate
                for candidate in self._resources.cameras
            )
            if existing is not None
            else (*self._resources.cameras, camera)
        )
        updated = replace(
            self._resources,
            revision=self._resources.revision + 1,
            cameras=cameras,
        )
        self._resource_repository.save(
            updated,
            expected_revision=self._resources.revision,
        )
        self._resources = updated
        active_source = next(
            (
                source
                for source in self._documents.document.sources
                if source.id == camera.id
            ),
            None,
        )
        if active_source is not None:
            self._documents.update_source(camera.id, camera)
        elif add_to_active_document:
            self._documents.create_source(camera)
        self._publish(SceneWorkspaceChangeKind.RESOURCES)
        return camera

    def add_configured_camera_to_active_document(self, camera_id: str) -> SourceDefinition:
        camera = self._resources.camera(camera_id)
        if any(source.id == camera.id for source in self._documents.document.sources):
            return self._documents.document.source(camera.id)
        self._documents.create_source(camera)
        return camera

    def create_camera_preset(self, preset: CameraPreset) -> CameraPreset:
        if any(candidate.id == preset.id for candidate in self._resources.camera_presets):
            raise SceneWorkspaceOperationError("PTZ preset already exists")
        self._resources.camera(preset.camera_source_id)
        updated = replace(
            self._resources,
            revision=self._resources.revision + 1,
            camera_presets=(*self._resources.camera_presets, preset),
        )
        self._save_resources(updated)
        self._documents.create_camera_preset(preset)
        self._publish(SceneWorkspaceChangeKind.RESOURCES)
        return preset

    def update_camera_preset(
        self,
        preset_id: str,
        preset: CameraPreset,
    ) -> CameraPreset:
        if preset.id != preset_id:
            raise SceneWorkspaceOperationError("A PTZ preset id cannot be changed")
        if not any(candidate.id == preset_id for candidate in self._resources.camera_presets):
            raise SceneWorkspaceOperationError("PTZ preset does not exist")
        self._resources.camera(preset.camera_source_id)
        updated = replace(
            self._resources,
            revision=self._resources.revision + 1,
            camera_presets=tuple(
                preset if candidate.id == preset_id else candidate
                for candidate in self._resources.camera_presets
            ),
        )
        self._save_resources(updated)
        if any(candidate.id == preset_id for candidate in self._documents.document.camera_presets):
            self._documents.update_camera_preset(preset_id, preset)
        self._publish(SceneWorkspaceChangeKind.RESOURCES)
        return preset

    def delete_camera_preset(self, preset_id: str) -> None:
        if not any(candidate.id == preset_id for candidate in self._resources.camera_presets):
            raise SceneWorkspaceOperationError("PTZ preset does not exist")
        for collection in self._catalog.collections:
            if collection.id == self._catalog.active_collection_id:
                documents = self._documents
            else:
                repository = self._document_repository(collection.id)
                documents = SceneDocumentService(
                    self._load_recovering(repository),
                    store=repository,
                )
            if any(candidate.id == preset_id for candidate in documents.document.camera_presets):
                documents.delete_camera_preset(preset_id, cascade=True)
        updated = replace(
            self._resources,
            revision=self._resources.revision + 1,
            camera_presets=tuple(
                preset
                for preset in self._resources.camera_presets
                if preset.id != preset_id
            ),
        )
        self._save_resources(updated)
        self._publish(SceneWorkspaceChangeKind.RESOURCES)

    def activate_collection(self, collection_id: str) -> SceneCollection:
        activation = self.prepare_collection_activation(collection_id)
        return self.commit_collection_activation(activation)

    def prepare_collection_activation(
        self,
        collection_id: str,
    ) -> SceneWorkspaceActivation:
        self._require_mutable_catalog("switch")
        target = self._catalog.collection(collection_id)
        if collection_id == self._catalog.active_collection_id:
            raise SceneWorkspaceOperationError("Scene profile is already active")
        repository = self._document_repository(collection_id)
        document = self._load_document_for_use(repository)
        documents = SceneDocumentService(document, store=repository)
        base_runtime = create_default_runtime_state(document)
        runtime_state = self._preserved_runtime_state(
            document,
            previous=self._runtime.state,
            current=base_runtime,
            automation_configured=documents.program_automation_configured,
        )
        runtime = SceneRuntimeService(documents, runtime_state)
        return SceneWorkspaceActivation(
            source_collection_id=self._catalog.active_collection_id,
            target=target,
            documents=documents,
            runtime=runtime,
            catalog_revision=self._catalog.revision,
        )

    def commit_collection_activation(
        self,
        activation: SceneWorkspaceActivation,
    ) -> SceneCollection:
        if activation.source_collection_id != self._catalog.active_collection_id:
            raise SceneWorkspaceOperationError("Scene profile activation is stale")
        if activation.catalog_revision != self._catalog.revision:
            raise SceneWorkspaceOperationError("Scene profile catalog changed during activation")
        collection_id = activation.target.id
        pending = replace(
            self._catalog,
            revision=self._catalog.revision + 1,
            pending_collection_id=collection_id,
        )
        previous_catalog = self._catalog
        previous_document = self._documents.document
        previous_state = self._runtime.state
        pending_persisted = False
        next_runtime: SceneRuntimeService | None = None
        next_persistence: SceneRuntimePersistenceQueue | None = None
        if not self._runtime_persistence.flush():
            activation.runtime.close()
            raise SceneWorkspaceOperationError(
                "Could not persist the current live scene state before switching profiles"
            )
        try:
            self._catalog_repository.save(
                pending,
                expected_revision=self._catalog.revision,
            )
            pending_persisted = True
            persisted_runtime = self._runtime_repository.load_or_create(
                activation.documents.document
            )
            target_state = replace(
                activation.runtime.state,
                revision=persisted_runtime.revision + 1,
            )
            self._runtime_repository.save(
                target_state,
                expected_revision=persisted_runtime.revision,
            )
            next_persistence = SceneRuntimePersistenceQueue(
                self._runtime_repository,
                target_state,
            )
            next_runtime = SceneRuntimeService(
                activation.documents,
                target_state,
                store=next_persistence,
            )
            committed = replace(
                pending,
                revision=pending.revision + 1,
                active_collection_id=collection_id,
                pending_collection_id="",
            )
            self._catalog_repository.save(committed, expected_revision=pending.revision)
        except Exception:  # noqa: BLE001 - profile activation transaction rollback boundary
            if next_runtime is not None:
                next_runtime.close()
            if next_persistence is not None:
                next_persistence.close()
            activation.runtime.close()
            if pending_persisted:
                self._rollback_failed_activation(
                    previous_catalog=previous_catalog,
                    previous_document=previous_document,
                    previous_state=previous_state,
                )
            raise

        previous_runtime = self._runtime
        previous_persistence = self._runtime_persistence
        activation.runtime.close()
        self._catalog = committed
        self._unsubscribe_documents()
        self._documents = activation.documents
        self._unsubscribe_documents = self._documents.subscribe(
            self._on_active_document_changed
        )
        self._runtime = next_runtime
        self._runtime_persistence = next_persistence
        previous_runtime.close()
        previous_persistence.close()
        self._publish(SceneWorkspaceChangeKind.ACTIVATED)
        return activation.target

    @staticmethod
    def discard_collection_activation(activation: SceneWorkspaceActivation) -> None:
        activation.runtime.close()

    def delete_collection(
        self,
        collection_id: str,
        *,
        replacement_collection_id: str | None = None,
    ) -> SceneCollectionCatalog:
        self._require_mutable_catalog("delete")
        self._catalog.collection(collection_id)
        if len(self._catalog.collections) == 1:
            raise SceneWorkspaceOperationError("The last Scene profile cannot be deleted")
        if collection_id == self._catalog.active_collection_id:
            if replacement_collection_id is None:
                raise SceneWorkspaceOperationError(
                    "Deleting the active Scene profile requires a replacement"
                )
            if replacement_collection_id == collection_id:
                raise SceneWorkspaceOperationError(
                    "The replacement Scene profile must be different"
                )
            self.activate_collection(replacement_collection_id)
        elif replacement_collection_id is not None:
            raise SceneWorkspaceOperationError(
                "A replacement is accepted only for the active Scene profile"
            )

        updated = replace(
            self._catalog,
            revision=self._catalog.revision + 1,
            collections=tuple(
                collection
                for collection in self._catalog.collections
                if collection.id != collection_id
            ),
        )
        self._save_catalog(updated)
        self._remove_collection_files(collection_id)
        self._collect_orphaned_cameras_safely()
        self._publish(SceneWorkspaceChangeKind.CATALOG)
        return updated

    def close(self) -> None:
        self._unsubscribe_documents()
        self._runtime.close()
        self._runtime_persistence.close()
        self._listeners.clear()

    def collect_orphaned_cameras(self) -> tuple[SourceDefinition, ...]:
        """Remove cameras only after current, backup, Undo, and Redo release them."""

        retained_ids = set(self._documents.retained_source_ids)
        candidates = {
            camera.id for camera in self._resources.cameras if camera.id not in retained_ids
        }
        if not candidates:
            return ()

        for collection in self._catalog.collections:
            repository = self._document_repository(collection.id)
            documents: list[SceneDocument] = []
            if collection.id != self._catalog.active_collection_id:
                try:
                    documents.append(repository.load())
                except SceneRepositoryCorruptError:
                    log.warning(
                        "Deferring camera cleanup because a Scene profile is corrupt",
                        exc_info=True,
                    )
                    return ()
            try:
                documents.append(repository.load_backup())
            except FileNotFoundError:
                pass
            except SceneRepositoryCorruptError:
                log.warning(
                    "Deferring camera cleanup because a Scene profile backup is corrupt",
                    exc_info=True,
                )
                return ()
            for document in documents:
                candidates.difference_update(source.id for source in document.sources)
            if not candidates:
                return ()

        removed = tuple(
            camera for camera in self._resources.cameras if camera.id in candidates
        )
        credential_refs = tuple(
            reference
            for camera in removed
            for reference in self._camera_credential_refs(camera)
        )
        pending_credentials = tuple(
            dict.fromkeys(
                (*self._resources.pending_credential_deletions, *credential_refs)
            )
        )
        updated = replace(
            self._resources,
            revision=self._resources.revision + 1,
            cameras=tuple(
                camera for camera in self._resources.cameras if camera.id not in candidates
            ),
            camera_presets=tuple(
                preset
                for preset in self._resources.camera_presets
                if preset.camera_source_id not in candidates
            ),
            pending_credential_deletions=pending_credentials,
        )
        self._save_resources(updated)
        self._flush_pending_credential_deletions()
        self._publish(SceneWorkspaceChangeKind.RESOURCES)
        return removed

    def _load_or_migrate_catalog(self) -> SceneCollectionCatalog:
        if self._catalog_repository.exists():
            catalog = self._catalog_repository.load()
            if not catalog.pending_collection_id:
                return catalog
            recovered = replace(
                catalog,
                revision=catalog.revision + 1,
                pending_collection_id="",
            )
            self._catalog_repository.save(recovered, expected_revision=catalog.revision)
            return recovered

        collection = SceneCollection(
            id=DEFAULT_SCENE_COLLECTION_ID,
            name="Profile 1",
        )
        destination = self._document_repository(collection.id)
        backup_document: SceneDocument | None = None
        legacy: SceneDocumentRepository | None = None
        if self._paths.scenes_file.exists():
            legacy = SceneDocumentRepository(
                self._paths.scenes_file,
                seed_factory=self._fresh_document,
            )
            try:
                backup_document = legacy.load_backup()
            except (FileNotFoundError, SceneRepositoryCorruptError):
                pass
        if not destination.path.exists():
            if legacy is not None:
                document = self._load_recovering(legacy)
            else:
                document = self._fresh_document()
            destination.save(document)
        else:
            document = self._load_recovering(destination)
        destination.initialize_backup(backup_document or document)

        catalog = SceneCollectionCatalog(
            revision=0,
            active_collection_id=collection.id,
            collections=(collection,),
        )
        self._catalog_repository.save(catalog)
        return catalog

    def _load_or_migrate_resources(self) -> SceneResourceCatalog:
        if self._resource_repository.exists():
            return self._resource_repository.load()
        ordered_collections = (
            self._catalog.active,
            *(
                collection
                for collection in self._catalog.collections
                if collection.id != self._catalog.active_collection_id
            ),
        )
        cameras: dict[str, SourceDefinition] = {}
        presets = {}
        for collection in ordered_collections:
            repository = self._document_repository(collection.id)
            documents = [self._load_recovering(repository)]
            try:
                documents.append(repository.load_backup())
            except FileNotFoundError:
                pass
            except SceneRepositoryCorruptError:
                log.warning(
                    "Ignoring corrupt Scene profile backup while loading shared resources",
                    exc_info=True,
                )
            for document in documents:
                for camera in cameras_from_document_sources(document.sources):
                    cameras.setdefault(camera.id, camera)
                for preset in document.camera_presets:
                    presets.setdefault(preset.id, preset)
        resources = SceneResourceCatalog(
            revision=0,
            cameras=tuple(cameras.values()),
            camera_presets=tuple(presets.values()),
        )
        self._resource_repository.save(resources)
        return resources

    def _fresh_document(self) -> SceneDocument:
        document = create_fresh_scene_collection_document(
            self._seed_names,
            document_id=self._identity_factory(),
        )
        if not hasattr(self, "_resources"):
            return document
        if not self._resources.cameras:
            return replace(
                document,
                sources=tuple(
                    source
                    for source in document.sources
                    if source.id != DEFAULT_CAMERA_SOURCE_ID
                ),
                scenes=tuple(
                    scene for scene in document.scenes if scene.id != CAMERA_SCENE_ID
                ),
                outputs=tuple(
                    replace(output, default_scene_id=CONTENT_SCENE_ID)
                    for output in document.outputs
                ),
            )
        camera = self._resources.cameras[0]
        return replace(
            document,
            sources=tuple(
                replace(camera, enabled=source.enabled)
                if source.id == DEFAULT_CAMERA_SOURCE_ID
                else source
                for source in document.sources
            ),
            scenes=tuple(
                replace(
                    scene,
                    layers=tuple(
                        replace(layer, source_id=camera.id)
                        if layer.source_id == DEFAULT_CAMERA_SOURCE_ID
                        else layer
                        for layer in scene.layers
                    ),
                )
                for scene in document.scenes
            ),
        )

    def _load_services(
        self,
        collection_id: str,
        *,
        previous_runtime: SceneRuntimeState | None,
    ) -> tuple[
        SceneDocumentService,
        SceneRuntimeService,
        SceneRuntimePersistenceQueue,
    ]:
        repository = self._document_repository(collection_id)
        document = self._load_document_for_use(repository)
        documents = SceneDocumentService(document, store=repository)
        runtime_state = self._runtime_repository.load_or_create(document)
        if previous_runtime is not None:
            runtime_state = self._preserved_runtime_state(
                document,
                previous=previous_runtime,
                current=runtime_state,
                automation_configured=documents.program_automation_configured,
            )
            self._runtime_repository.save(
                runtime_state,
                expected_revision=runtime_state.revision - 1,
            )
        persistence = SceneRuntimePersistenceQueue(
            self._runtime_repository,
            runtime_state,
        )
        return (
            documents,
            SceneRuntimeService(documents, runtime_state, store=persistence),
            persistence,
        )

    def _load_document_for_use(
        self,
        repository: SceneDocumentRepository | SceneCollectionDocumentRepository,
    ) -> SceneDocument:
        """Load a collection document ready to drive a live SceneDocumentService.

        Materializes the shared camera resources, then self-heals collections
        saved before idle scenes by adding the Default scene when it is missing.
        """
        document = ensure_default_scene(
            self._materialize_resources(self._load_recovering(repository)),
            self._seed_names,
        )
        return self._localize_idle_source_names(document)

    def _localize_idle_source_names(self, document: SceneDocument) -> SceneDocument:
        """Localize the codec's generic migration label without changing custom layers."""
        sources = tuple(
            replace(source, name=self._seed_names.idle_screen_source)
            if source.kind is SourceKind.IDLE_SCREEN and source.name == "Idle screen"
            else source
            for source in document.sources
        )
        renamed_ids = {
            source.id for source, original in zip(sources, document.sources, strict=True)
            if source != original
        }
        if not renamed_ids:
            return document
        scenes = tuple(
            replace(
                scene,
                layers=tuple(
                    replace(layer, name=self._seed_names.idle_screen_layer)
                    if layer.source_id in renamed_ids and layer.name == "Idle screen"
                    else layer
                    for layer in scene.layers
                ),
            )
            for scene in document.scenes
        )
        return replace(document, sources=sources, scenes=scenes)

    def _materialize_resources(self, document: SceneDocument) -> SceneDocument:
        camera_by_id = {camera.id: camera for camera in self._resources.cameras}
        sources = tuple(camera_by_id.get(source.id, source) for source in document.sources)
        active_camera_ids = {
            source.id
            for source in sources
            if source.kind in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}
        }
        presets = tuple(
            preset
            for preset in self._resources.camera_presets
            if preset.camera_source_id in active_camera_ids
        )
        return replace(document, sources=sources, camera_presets=presets)

    @staticmethod
    def _preserved_runtime_state(
        document: SceneDocument,
        *,
        previous: SceneRuntimeState,
        current: SceneRuntimeState,
        automation_configured: bool,
    ) -> SceneRuntimeState:
        default_scene_id = (
            document.output(BusId.VIRTUAL_CAMERA).default_scene_id
            or document.scenes[0].id
        )
        previous_primary = previous.output(BusId.VIRTUAL_CAMERA)
        mode = (
            previous_primary.mode
            if previous_primary.mode is OutputMode.MANUAL or automation_configured
            else OutputMode.MANUAL
        )
        outputs = tuple(
            OutputRuntimeState(
                bus_id=output.bus_id,
                mode=mode,
                manual_scene_id=default_scene_id,
                enabled=previous.output(output.bus_id).enabled,
            )
            for output in current.outputs
        )
        return replace(current, revision=current.revision + 1, outputs=outputs)

    def _document_repository(
        self,
        collection_id: str,
    ) -> SceneDocumentRepository | SceneCollectionDocumentRepository:
        path = self._collection_path(collection_id)
        if hasattr(self, "_resources"):
            return SceneCollectionDocumentRepository(
                path,
                resources=lambda: self._resources,
            )
        return SceneDocumentRepository(path, seed_factory=self._fresh_document)

    def _normalize_collection_storage(self) -> None:
        for collection in self._catalog.collections:
            repository = self._document_repository(collection.id)
            if isinstance(repository, SceneCollectionDocumentRepository):
                try:
                    repository.normalize_storage()
                except SceneRepositoryCorruptError:
                    repository.recover_from_backup()
                    repository.normalize_storage()

    @staticmethod
    def _load_recovering(
        repository: SceneDocumentRepository | SceneCollectionDocumentRepository,
    ) -> SceneDocument:
        try:
            return repository.load()
        except SceneRepositoryCorruptError:
            log.warning(
                "Recovering a corrupt Scene profile from its last known-good backup",
                exc_info=True,
            )
            return repository.recover_from_backup()

    def _rollback_failed_activation(
        self,
        *,
        previous_catalog: SceneCollectionCatalog,
        previous_document: SceneDocument,
        previous_state: SceneRuntimeState,
    ) -> None:
        """Best-effort rollback for a failed local commit after native hydration."""

        try:
            persisted_catalog = self._catalog_repository.load()
            if persisted_catalog != previous_catalog:
                recovered_catalog = replace(
                    previous_catalog,
                    revision=persisted_catalog.revision + 1,
                    pending_collection_id="",
                )
                self._catalog_repository.save(
                    recovered_catalog,
                    expected_revision=persisted_catalog.revision,
                )
                self._catalog = recovered_catalog
        except Exception:  # noqa: BLE001 - preserve the original commit failure
            log.exception("Could not roll back the pending Scene profile activation")
        try:
            persisted_runtime = self._runtime_repository.load_or_create(previous_document)
            if persisted_runtime != previous_state:
                self._runtime_repository.save(
                    previous_state,
                    expected_revision=persisted_runtime.revision,
                )
        except Exception:  # noqa: BLE001 - preserve the original commit failure
            log.exception("Could not roll back Scene runtime state after activation failure")

    def _collection_path(self, collection_id: str) -> Path:
        return self._paths.scene_profiles_dir / f"{collection_id}.json"

    def _remove_collection_files(self, collection_id: str) -> None:
        primary = self._collection_path(collection_id)
        backup = primary.with_name(f"{primary.stem}.backup{primary.suffix}")
        lock = primary.with_suffix(f"{primary.suffix}.lock")
        for path in (primary, backup, lock):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove deleted Scene profile data", exc_info=True)

    def _require_mutable_catalog(self, operation: str) -> None:
        if self.virtual_camera_enabled:
            raise SceneWorkspaceBusyError(
                f"Turn off the Solin Virtual Camera to {operation} Scene profiles"
            )
        for guard in tuple(self._catalog_mutation_guards):
            reason = guard(operation)
            if reason:
                raise SceneWorkspaceBusyError(reason)

    def _save_catalog(self, catalog: SceneCollectionCatalog) -> None:
        self._catalog_repository.save(
            catalog,
            expected_revision=self._catalog.revision,
        )
        self._catalog = catalog

    def _save_resources(self, resources: SceneResourceCatalog) -> None:
        self._resource_repository.save(
            resources,
            expected_revision=self._resources.revision,
        )
        self._resources = resources

    def _flush_pending_credential_deletions(self) -> None:
        cleaner = self._credential_cleaner
        if cleaner is None or not self._resources.pending_credential_deletions:
            return
        retained: list[str] = []
        for credential_ref in self._resources.pending_credential_deletions:
            try:
                cleaner(credential_ref)
            except Exception:  # noqa: BLE001 - OS credential-store boundary
                retained.append(credential_ref)
                log.warning(
                    "Could not remove orphaned PTZ credentials; cleanup remains queued",
                    exc_info=True,
                )
        remaining = tuple(retained)
        if remaining == self._resources.pending_credential_deletions:
            return
        self._save_resources(
            replace(
                self._resources,
                revision=self._resources.revision + 1,
                pending_credential_deletions=remaining,
            )
        )

    def _on_active_document_changed(self, _change: object) -> None:
        self._collect_orphaned_cameras_safely()

    def _collect_orphaned_cameras_safely(self) -> None:
        try:
            self.collect_orphaned_cameras()
        except Exception:  # noqa: BLE001 - cleanup must not break committed edits
            log.exception("Could not collect orphaned shared scene cameras")

    @staticmethod
    def _camera_credential_refs(camera: SourceDefinition) -> tuple[str, ...]:
        """Every vault entry this camera owns, so deleting it strands nothing.

        A camera can hold two independent logins: the PTZ account on its binding
        and the stream account on the source itself. They are routinely different,
        and harvesting only the first left the other in the keyring forever with
        nothing left to reference it.
        """
        references: list[str] = []
        stream_ref = camera.credential_ref
        if isinstance(stream_ref, str) and stream_ref:
            references.append(stream_ref)
        configuration = camera.configuration
        if isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
            ptz_ref = getattr(configuration.ptz_binding, "credential_ref", "")
            if isinstance(ptz_ref, str) and ptz_ref:
                references.append(ptz_ref)
        return tuple(references)

    def _publish(self, kind: SceneWorkspaceChangeKind) -> None:
        change = SceneWorkspaceChange(
            kind=kind,
            catalog=self._catalog,
            collection=self._catalog.active,
            document=self._documents.document,
        )
        for listener in tuple(self._listeners):
            try:
                listener(change)
            except Exception:  # noqa: BLE001 - observer boundary
                log.warning("Scene workspace listener failed", exc_info=True)
