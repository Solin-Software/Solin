from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
import logging
from typing import Protocol

from solin.core.scenes.model import (
    AUTOMATIC_MEDIA_CATEGORIES,
    CONTENT_SOURCE_ID,
    MAX_LAYERS_PER_SCENE,
    NO_SIGNAL_SOURCE_ID,
    BusId,
    CameraPreset,
    Crop,
    FitMode,
    NormalizedRect,
    RecallPtzPresetAction,
    SceneDefinition,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
    new_identity,
    utc_now_iso,
)
from solin.core.scenes.presets import CONTENT_SCENE_ID

log = logging.getLogger(__name__)


class SceneOperationError(RuntimeError):
    pass


class SceneNotFoundError(SceneOperationError):
    pass


class SourceNotFoundError(SceneOperationError):
    pass


class CameraPresetNotFoundError(SceneOperationError):
    pass


class SceneConflictError(SceneOperationError):
    pass


class SceneHistoryEmptyError(SceneOperationError):
    pass


class SceneEditConflictError(SceneOperationError):
    pass


class SceneDocumentStore(Protocol):
    def save(
        self,
        document: SceneDocument,
        *,
        expected_revision: int | None = None,
    ) -> None: ...


class SceneChangeKind(StrEnum):
    CONFIGURATION = "configuration"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class SceneDocumentChange:
    document: SceneDocument
    kind: SceneChangeKind


@dataclass(slots=True)
class SceneEditDraft:
    base_revision: int
    scene: SceneDefinition

    def update_layer(self, layer_id: str, layer: SceneLayer) -> SceneDefinition:
        if layer.id != layer_id:
            raise SceneConflictError("A layer id cannot be changed")
        if not any(candidate.id == layer_id for candidate in self.scene.layers):
            raise SceneNotFoundError(f"Layer does not exist: {layer_id}")
        self.scene = replace(
            self.scene,
            layers=tuple(
                layer if candidate.id == layer_id else candidate for candidate in self.scene.layers
            ),
        )
        return self.scene


class SceneDocumentService:
    """Transactional editor for an immutable scene document."""

    def __init__(
        self,
        document: SceneDocument,
        *,
        clock: Callable[[], str] = utc_now_iso,
        identity_factory: Callable[[], str] = new_identity,
        store: SceneDocumentStore | None = None,
        history_limit: int = 100,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._document = document
        self._clock = clock
        self._identity_factory = identity_factory
        self._store = store
        self._history_limit = history_limit
        self._undo: list[SceneDocument] = []
        self._redo: list[SceneDocument] = []
        self._listeners: set[Callable[[SceneDocumentChange], None]] = set()

    @property
    def document(self) -> SceneDocument:
        return self._document

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def retained_source_ids(self) -> frozenset[str]:
        """Source identities retained by current state or local Undo/Redo history."""

        return frozenset(
            source.id
            for document in (self._document, *self._undo, *self._redo)
            for source in document.sources
        )

    def subscribe(
        self,
        listener: Callable[[SceneDocumentChange], None],
    ) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def create_scene(self, name: str, *, scene_id: str | None = None) -> SceneDocument:
        timestamp = self._clock()
        scene = SceneDefinition(
            id=scene_id or self._identity_factory(),
            name=name,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self._commit(replace(self._document, scenes=(*self._document.scenes, scene)))

    def begin_scene_edit(self, scene_id: str) -> SceneEditDraft:
        return SceneEditDraft(
            base_revision=self._document.revision,
            scene=self._scene(scene_id),
        )

    def commit_scene_edit(self, draft: SceneEditDraft) -> SceneDocument:
        if draft.base_revision != self._document.revision:
            raise SceneEditConflictError(
                "The scene document changed while the editor draft was active"
            )
        return self.update_scene(draft.scene.id, draft.scene)

    def duplicate_scene(
        self,
        scene_id: str,
        *,
        name: str | None = None,
        new_scene_id: str | None = None,
    ) -> SceneDocument:
        source = self._scene(scene_id)
        timestamp = self._clock()
        duplicate = replace(
            source,
            id=new_scene_id or self._identity_factory(),
            name=name or source.name,
            layers=tuple(replace(layer, id=self._identity_factory()) for layer in source.layers),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return self._commit(replace(self._document, scenes=(*self._document.scenes, duplicate)))

    def update_scene(self, scene_id: str, scene: SceneDefinition) -> SceneDocument:
        current = self._scene(scene_id)
        if scene.id != scene_id:
            raise SceneConflictError("A scene id cannot be changed")
        updated = replace(
            scene,
            created_at=current.created_at,
            updated_at=self._clock(),
        )
        return self._commit(self._document_with_scene(updated))

    def rename_scene(self, scene_id: str, name: str) -> SceneDocument:
        scene = self._scene(scene_id)
        return self.update_scene(scene_id, replace(scene, name=name))

    def reorder_scene(self, scene_id: str, index: int) -> SceneDocument:
        scene = self._scene(scene_id)
        scenes = [candidate for candidate in self._document.scenes if candidate.id != scene_id]
        if not 0 <= index <= len(scenes):
            raise IndexError("Scene index is out of range")
        scenes.insert(index, scene)
        return self._commit(replace(self._document, scenes=tuple(scenes)))

    def delete_scene(self, scene_id: str) -> SceneDocument:
        self._scene(scene_id)
        if len(self._document.scenes) == 1:
            raise SceneConflictError("The last scene cannot be deleted")
        scene_reference_source_ids = {
            source.id
            for source in self._document.sources
            if source.kind is SourceKind.SCENE_REFERENCE
            and isinstance(source.configuration, SceneReferenceConfig)
            and source.configuration.target_scene_id == scene_id
        }
        timestamp = self._clock()
        outputs = tuple(
            replace(
                route,
                default_scene_id="" if route.default_scene_id == scene_id else route.default_scene_id,
            )
            for route in self._document.outputs
        )
        automation = tuple(
            replace(
                mapping,
                assignments=tuple(
                    (category, assigned_scene_id)
                    for category, assigned_scene_id in mapping.assignments
                    if assigned_scene_id != scene_id
                ),
            )
            for mapping in self._document.automation
        )
        sources = tuple(
            source
            for source in self._document.sources
            if source.id not in scene_reference_source_ids
        )
        scenes = tuple(
            self._without_source_references(
                scene,
                source_ids=scene_reference_source_ids,
                preset_ids=set(),
                timestamp=timestamp,
            )
            for scene in self._document.scenes
            if scene.id != scene_id
        )
        return self._commit(
            replace(
                self._document,
                sources=sources,
                scenes=scenes,
                outputs=outputs,
                automation=automation,
            )
        )

    def create_source(self, source: SourceDefinition) -> SceneDocument:
        if any(candidate.id == source.id for candidate in self._document.sources):
            raise SceneConflictError(f"Source already exists: {source.id}")
        return self._commit(replace(self._document, sources=(*self._document.sources, source)))

    def update_source(self, source_id: str, source: SourceDefinition) -> SceneDocument:
        self._source(source_id)
        if source.id != source_id:
            raise SceneConflictError("A source id cannot be changed")
        sources = tuple(
            source if candidate.id == source_id else candidate
            for candidate in self._document.sources
        )
        return self._commit(replace(self._document, sources=sources))

    def delete_source(self, source_id: str, *, cascade: bool = False) -> SceneDocument:
        self._source(source_id)
        if source_id in {CONTENT_SOURCE_ID, NO_SIGNAL_SOURCE_ID}:
            raise SceneConflictError("A required built-in source cannot be deleted")
        referenced_preset_ids = {
            preset.id
            for preset in self._document.camera_presets
            if preset.camera_source_id == source_id
        }
        layer_references = any(
            layer.source_id == source_id
            for scene in self._document.scenes
            for layer in scene.layers
        )
        if (layer_references or referenced_preset_ids) and not cascade:
            raise SceneConflictError("A referenced source requires cascade deletion")

        timestamp = self._clock()
        scenes = tuple(
            self._without_source_references(
                scene,
                source_ids={source_id},
                preset_ids=referenced_preset_ids,
                timestamp=timestamp,
            )
            for scene in self._document.scenes
        )
        return self._commit(
            replace(
                self._document,
                sources=tuple(
                    source for source in self._document.sources if source.id != source_id
                ),
                scenes=scenes,
                camera_presets=tuple(
                    preset
                    for preset in self._document.camera_presets
                    if preset.id not in referenced_preset_ids
                ),
            )
        )

    def add_layer(
        self,
        scene_id: str,
        layer: SceneLayer,
        *,
        index: int | None = None,
    ) -> SceneDocument:
        self._source(layer.source_id)
        scene = self._scene(scene_id)
        if len(scene.layers) >= MAX_LAYERS_PER_SCENE:
            raise SceneConflictError("Scene has reached the layer limit")
        if any(candidate.id == layer.id for candidate in scene.layers):
            raise SceneConflictError(f"Layer already exists: {layer.id}")
        layers = list(scene.layers)
        insertion_index = len(layers) if index is None else index
        if not 0 <= insertion_index <= len(layers):
            raise IndexError("Layer index is out of range")
        layers.insert(insertion_index, layer)
        return self.update_scene(scene_id, replace(scene, layers=tuple(layers)))

    def update_layer(
        self,
        scene_id: str,
        layer_id: str,
        layer: SceneLayer,
    ) -> SceneDocument:
        self._source(layer.source_id)
        scene = self._scene(scene_id)
        self._layer(scene, layer_id)
        if layer.id != layer_id:
            raise SceneConflictError("A layer id cannot be changed")
        layers = tuple(
            layer if candidate.id == layer_id else candidate for candidate in scene.layers
        )
        return self.update_scene(scene_id, replace(scene, layers=layers))

    def delete_layer(self, scene_id: str, layer_id: str) -> SceneDocument:
        scene = self._scene(scene_id)
        self._layer(scene, layer_id)
        layers = tuple(layer for layer in scene.layers if layer.id != layer_id)
        return self.update_scene(scene_id, replace(scene, layers=layers))

    def delete_layer_and_orphaned_source(
        self,
        scene_id: str,
        layer_id: str,
    ) -> SceneDocument:
        """Remove a layer and its private source when no document reference remains."""

        scene = self._scene(scene_id)
        layer = self._layer(scene, layer_id)
        timestamp = self._clock()
        updated_scene = replace(
            scene,
            layers=tuple(candidate for candidate in scene.layers if candidate.id != layer_id),
            updated_at=timestamp,
        )
        scenes = tuple(
            updated_scene if candidate.id == scene_id else candidate
            for candidate in self._document.scenes
        )
        source_is_required = layer.source_id in {CONTENT_SOURCE_ID, NO_SIGNAL_SOURCE_ID}
        source_is_referenced = any(
            candidate.source_id == layer.source_id
            for candidate_scene in scenes
            for candidate in candidate_scene.layers
        )
        source_has_presets = any(
            preset.camera_source_id == layer.source_id
            for preset in self._document.camera_presets
        )
        sources = (
            self._document.sources
            if source_is_required or source_is_referenced or source_has_presets
            else tuple(
                source
                for source in self._document.sources
                if source.id != layer.source_id
            )
        )
        return self._commit(replace(self._document, scenes=scenes, sources=sources))

    def reorder_layer(self, scene_id: str, layer_id: str, index: int) -> SceneDocument:
        scene = self._scene(scene_id)
        layer = self._layer(scene, layer_id)
        layers = [candidate for candidate in scene.layers if candidate.id != layer_id]
        if not 0 <= index <= len(layers):
            raise IndexError("Layer index is out of range")
        layers.insert(index, layer)
        return self.update_scene(scene_id, replace(scene, layers=tuple(layers)))

    def create_camera_preset(self, preset: CameraPreset) -> SceneDocument:
        if any(candidate.id == preset.id for candidate in self._document.camera_presets):
            raise SceneConflictError(f"Camera preset already exists: {preset.id}")
        self._camera_source(preset.camera_source_id)
        return self._commit(
            replace(
                self._document,
                camera_presets=(*self._document.camera_presets, preset),
            )
        )

    def update_camera_preset(
        self,
        preset_id: str,
        preset: CameraPreset,
    ) -> SceneDocument:
        current = self._preset(preset_id)
        if preset.id != preset_id:
            raise SceneConflictError("A camera preset id cannot be changed")
        self._camera_source(preset.camera_source_id)
        updated = replace(
            preset,
            created_at=current.created_at,
            updated_at=self._clock(),
        )
        presets = tuple(
            updated if candidate.id == preset_id else candidate
            for candidate in self._document.camera_presets
        )
        return self._commit(replace(self._document, camera_presets=presets))

    def delete_camera_preset(
        self,
        preset_id: str,
        *,
        cascade: bool = False,
    ) -> SceneDocument:
        self._preset(preset_id)
        is_referenced = any(
            action.preset_id == preset_id
            for scene in self._document.scenes
            for action in scene.entry_actions
        )
        if is_referenced and not cascade:
            raise SceneConflictError("A referenced camera preset requires cascade deletion")
        timestamp = self._clock()
        scenes = tuple(
            self._without_preset_action(scene, preset_id, timestamp)
            for scene in self._document.scenes
        )
        presets = tuple(
            preset for preset in self._document.camera_presets if preset.id != preset_id
        )
        return self._commit(replace(self._document, scenes=scenes, camera_presets=presets))

    def set_scene_entry_action(
        self,
        scene_id: str,
        action: RecallPtzPresetAction,
    ) -> SceneDocument:
        self._preset(action.preset_id)
        scene = self._scene(scene_id)
        actions = tuple(
            candidate
            for candidate in scene.entry_actions
            if candidate.preset_id != action.preset_id
        ) + (action,)
        return self.update_scene(scene_id, replace(scene, entry_actions=actions))

    def delete_scene_entry_action(
        self,
        scene_id: str,
        preset_id: str,
    ) -> SceneDocument:
        scene = self._scene(scene_id)
        if not any(action.preset_id == preset_id for action in scene.entry_actions):
            raise CameraPresetNotFoundError(
                f"Scene entry action does not exist for preset: {preset_id}"
            )
        actions = tuple(action for action in scene.entry_actions if action.preset_id != preset_id)
        return self.update_scene(scene_id, replace(scene, entry_actions=actions))

    def set_program_default_scene(self, scene_id: str | None) -> SceneDocument:
        """Set the base Program scene for both delivery destinations atomically."""

        if scene_id is not None:
            self._scene(scene_id)
        media_scene_id = self.program_media_scene_id
        if scene_id is not None and media_scene_id == scene_id:
            raise SceneConflictError("Default and media scenes must be different")
        stored_scene_id = scene_id or ""
        return self._commit(
            replace(
                self._document,
                outputs=tuple(
                    replace(route, default_scene_id=stored_scene_id)
                    for route in self._document.outputs
                ),
            )
        )

    def set_program_media_scene(self, scene_id: str | None) -> SceneDocument:
        """Map all projected-media categories to one Program scene atomically."""

        if scene_id is not None:
            self._scene(scene_id)
        if scene_id is not None and self.program_default_scene_id == scene_id:
            raise SceneConflictError("Default and media scenes must be different")
        automation = tuple(
            replace(
                mapping,
                assignments=(
                    tuple(
                        (category, assigned_scene_id)
                        for category, assigned_scene_id in mapping.assignments
                        if category not in AUTOMATIC_MEDIA_CATEGORIES
                    )
                    + (
                        tuple(
                            (category, scene_id)
                            for category in AUTOMATIC_MEDIA_CATEGORIES
                        )
                        if scene_id is not None
                        else ()
                    )
                ),
            )
            for mapping in self._document.automation
        )
        return self._commit(replace(self._document, automation=automation))

    @property
    def program_default_scene_id(self) -> str | None:
        return self._document.output(BusId.VIRTUAL_CAMERA).default_scene_id or None

    @property
    def program_media_scene_id(self) -> str | None:
        mapping = self._document.automation_for(BusId.VIRTUAL_CAMERA)
        assigned = tuple(mapping.scene_for(category) for category in AUTOMATIC_MEDIA_CATEGORIES)
        if not assigned or assigned[0] is None or any(value != assigned[0] for value in assigned):
            return None
        return assigned[0]

    @property
    def direct_content_scene_id(self) -> str | None:
        """Return the canonical, visually unmodified Solin-content scene."""

        source = next(
            (candidate for candidate in self._document.sources if candidate.id == CONTENT_SOURCE_ID),
            None,
        )
        scene = next(
            (candidate for candidate in self._document.scenes if candidate.id == CONTENT_SCENE_ID),
            None,
        )
        if (
            source is None
            or source.kind is not SourceKind.SOLIN_CONTENT
            or not source.enabled
            or scene is None
            or len(scene.layers) != 1
        ):
            return None
        layer = scene.layers[0]
        if (
            layer.source_id != CONTENT_SOURCE_ID
            or layer.rect != NormalizedRect()
            or layer.crop != Crop()
            or layer.rotation_degrees != 0.0
            or layer.fit_mode is not FitMode.CONTAIN
            or layer.opacity != 1.0
            or not layer.visible
            or layer.mirror_x
            or layer.mirror_y
            or layer.border_width != 0.0
            or layer.corner_radius != 0.0
        ):
            return None
        return scene.id

    @property
    def program_automation_configured(self) -> bool:
        media_scene_id = self.program_media_scene_id
        return (
            self.program_default_scene_id is not None
            and media_scene_id is not None
            and media_scene_id != self.program_default_scene_id
        )

    def undo(self) -> SceneDocument:
        if not self._undo:
            raise SceneHistoryEmptyError("There is no scene edit to undo")
        snapshot = self._undo[-1]
        restored = replace(snapshot, revision=self._document.revision + 1)
        self._persist_if_configured(restored, self._document.revision)
        self._undo.pop()
        self._append_history(self._redo, self._document)
        self._document = restored
        self._publish_changed(SceneChangeKind.HISTORY)
        return self._document

    def redo(self) -> SceneDocument:
        if not self._redo:
            raise SceneHistoryEmptyError("There is no scene edit to redo")
        snapshot = self._redo[-1]
        restored = replace(snapshot, revision=self._document.revision + 1)
        self._persist_if_configured(restored, self._document.revision)
        self._redo.pop()
        self._append_history(self._undo, self._document)
        self._document = restored
        self._publish_changed(SceneChangeKind.HISTORY)
        return self._document

    def _commit(self, candidate: SceneDocument) -> SceneDocument:
        if candidate.document_id != self._document.document_id:
            raise SceneConflictError("A document id cannot be changed")
        if replace(candidate, revision=self._document.revision) == self._document:
            return self._document
        updated = replace(candidate, revision=self._document.revision + 1)
        self._persist_if_configured(updated, self._document.revision)
        self._append_history(self._undo, self._document)
        self._redo.clear()
        self._document = updated
        self._publish_changed(SceneChangeKind.CONFIGURATION)
        return self._document

    def _persist_if_configured(
        self,
        document: SceneDocument,
        expected_revision: int,
    ) -> None:
        if self._store is not None:
            self._store.save(document, expected_revision=expected_revision)

    def _publish_changed(self, kind: SceneChangeKind) -> None:
        change = SceneDocumentChange(self._document, kind)
        for listener in tuple(self._listeners):
            try:
                listener(change)
            except Exception:  # noqa: BLE001 - application observer boundary
                log.warning("Scene document listener failed", exc_info=True)

    def _append_history(
        self,
        history: list[SceneDocument],
        document: SceneDocument,
    ) -> None:
        history.append(document)
        del history[: -self._history_limit]

    def _document_with_scene(self, updated: SceneDefinition) -> SceneDocument:
        return replace(
            self._document,
            scenes=tuple(
                updated if scene.id == updated.id else scene for scene in self._document.scenes
            ),
        )

    def _scene(self, scene_id: str) -> SceneDefinition:
        scene = next(
            (scene for scene in self._document.scenes if scene.id == scene_id),
            None,
        )
        if scene is None:
            raise SceneNotFoundError(f"Scene does not exist: {scene_id}")
        return scene

    def _source(self, source_id: str) -> SourceDefinition:
        source = next(
            (source for source in self._document.sources if source.id == source_id),
            None,
        )
        if source is None:
            raise SourceNotFoundError(f"Source does not exist: {source_id}")
        return source

    def _camera_source(self, source_id: str) -> SourceDefinition:
        source = self._source(source_id)
        if source.kind not in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}:
            raise SceneConflictError(f"Source is not a camera: {source_id}")
        return source

    def _preset(self, preset_id: str) -> CameraPreset:
        preset = next(
            (preset for preset in self._document.camera_presets if preset.id == preset_id),
            None,
        )
        if preset is None:
            raise CameraPresetNotFoundError(f"Camera preset does not exist: {preset_id}")
        return preset

    @staticmethod
    def _layer(scene: SceneDefinition, layer_id: str) -> SceneLayer:
        layer = next((layer for layer in scene.layers if layer.id == layer_id), None)
        if layer is None:
            raise SceneNotFoundError(f"Layer does not exist: {layer_id}")
        return layer

    @staticmethod
    def _without_source_references(
        scene: SceneDefinition,
        *,
        source_ids: set[str],
        preset_ids: set[str],
        timestamp: str,
    ) -> SceneDefinition:
        layers = tuple(layer for layer in scene.layers if layer.source_id not in source_ids)
        actions = tuple(
            action for action in scene.entry_actions if action.preset_id not in preset_ids
        )
        if layers == scene.layers and actions == scene.entry_actions:
            return scene
        return replace(
            scene,
            layers=layers,
            entry_actions=actions,
            updated_at=timestamp,
        )

    @staticmethod
    def _without_preset_action(
        scene: SceneDefinition,
        preset_id: str,
        timestamp: str,
    ) -> SceneDefinition:
        actions = tuple(action for action in scene.entry_actions if action.preset_id != preset_id)
        if actions == scene.entry_actions:
            return scene
        return replace(scene, entry_actions=actions, updated_at=timestamp)
