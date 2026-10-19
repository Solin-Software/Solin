from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import logging
from typing import Any, Protocol

from solin.core.scenes.application import SceneDocumentChange, SceneDocumentService
from solin.core.scenes.model import (
    AUTOMATIC_MEDIA_CATEGORIES,
    DELIVERY_BUSES,
    BusId,
    ContentCategory,
    OutputMode,
    SceneDocument,
    SceneValidationError,
)

log = logging.getLogger(__name__)


class SceneRuntimeStore(Protocol):
    def save(
        self,
        state: SceneRuntimeState,
        *,
        expected_revision: int | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OutputRuntimeState:
    bus_id: BusId
    base_scene_id: str
    mode: OutputMode = OutputMode.AUTO
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, BusId):
            raise SceneValidationError("Invalid runtime output bus")
        if not isinstance(self.mode, OutputMode):
            raise SceneValidationError("Invalid runtime output mode")
        if not isinstance(self.base_scene_id, str) or not self.base_scene_id or len(self.base_scene_id) > 128:
            raise SceneValidationError("Invalid runtime base scene id")
        if not isinstance(self.enabled, bool):
            raise SceneValidationError("Runtime output enabled must be a boolean")

    def to_record(self) -> dict[str, object]:
        return {
            "bus_id": self.bus_id.value,
            "mode": self.mode.value,
            "base_scene_id": self.base_scene_id,
            "enabled": self.enabled,
        }

    @classmethod
    def from_record(cls, raw: object) -> OutputRuntimeState:
        data = _strict_mapping(
            raw,
            field_name="runtime output",
            allowed_keys={"bus_id", "mode", "base_scene_id", "enabled"},
        )
        try:
            bus_id = BusId(data.get("bus_id"))
            mode = OutputMode(data.get("mode", OutputMode.AUTO.value))
        except (TypeError, ValueError) as exc:
            raise SceneValidationError("Invalid runtime output enum") from exc
        base_scene_id = data.get("base_scene_id", "")
        enabled = data.get("enabled", False)
        if not isinstance(base_scene_id, str) or not isinstance(enabled, bool):
            raise SceneValidationError("Invalid runtime output value")
        return cls(
            bus_id=bus_id,
            mode=mode,
            base_scene_id=base_scene_id,
            enabled=enabled,
        )


@dataclass(frozen=True, slots=True)
class SceneRuntimeState:
    document_id: str
    revision: int
    outputs: tuple[OutputRuntimeState, ...]
    schema_version: int = 4

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id:
            raise SceneValidationError("Invalid runtime document id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise SceneValidationError("Invalid runtime revision")
        if self.schema_version != 4:
            raise SceneValidationError("Unsupported runtime schema version")
        if not isinstance(self.outputs, tuple) or not all(
            isinstance(output, OutputRuntimeState) for output in self.outputs
        ):
            raise SceneValidationError("Runtime outputs must be an immutable tuple")
        buses = tuple(output.bus_id for output in self.outputs)
        if len(buses) != len(DELIVERY_BUSES) or set(buses) != set(DELIVERY_BUSES):
            raise SceneValidationError("Runtime state must define every output bus once")
        # Each delivery output keeps its own selection, so the projection and the
        # program can sit on different scenes.

    def output(self, bus_id: BusId) -> OutputRuntimeState:
        return next(output for output in self.outputs if output.bus_id is bus_id)

    def validate_against(self, document: SceneDocument) -> None:
        if self.document_id != document.document_id:
            raise SceneValidationError("Runtime state belongs to a different scene document")
        scene_ids = {scene.id for scene in document.scenes}
        for output in self.outputs:
            if output.base_scene_id not in scene_ids:
                raise SceneValidationError(
                    f"Runtime output {output.bus_id.value} references a missing scene"
                )

    def reconciled_against(self, document: SceneDocument) -> SceneRuntimeState:
        """Return a usable state after configuration changed while Solin was offline."""
        if self.document_id != document.document_id:
            return create_initial_runtime_state(document)
        scene_ids = {scene.id for scene in document.scenes}
        outputs = tuple(
            (
                replace(output, base_scene_id=document.scenes[0].id)
                if output.base_scene_id and output.base_scene_id not in scene_ids
                else output
            )
            for output in self.outputs
        )
        if outputs == self.outputs:
            return self
        return replace(self, revision=self.revision + 1, outputs=outputs)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "revision": self.revision,
            "outputs": [output.to_record() for output in self.outputs],
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneRuntimeState:
        data = _strict_mapping(
            raw,
            field_name="scene runtime state",
            allowed_keys={"schema_version", "document_id", "revision", "outputs"},
        )
        schema_version = data.get("schema_version")
        revision = data.get("revision")
        document_id = data.get("document_id")
        outputs = data.get("outputs")
        if schema_version != 4:
            raise SceneValidationError("Unsupported runtime schema version")
        if (
            not isinstance(document_id, str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(outputs, list)
        ):
            raise SceneValidationError("Invalid scene runtime state")
        restored_outputs = tuple(OutputRuntimeState.from_record(item) for item in outputs)
        return cls(
            document_id=document_id,
            revision=revision,
            outputs=restored_outputs,
            schema_version=4,
        )


def create_initial_runtime_state(
    document: SceneDocument,
    *,
    selections: Mapping[BusId, str] | None = None,
) -> SceneRuntimeState:
    from solin.core.scenes.presets import CAMERA_SCENE_ID, CONTENT_SCENE_ID

    scene_ids = {scene.id for scene in document.scenes}
    initial = {
        BusId.MEDIA_WINDOWS: CONTENT_SCENE_ID,
        BusId.VIRTUAL_CAMERA: CAMERA_SCENE_ID,
    }
    if selections is not None:
        initial.update(selections)
    state = SceneRuntimeState(
        document_id=document.document_id,
        revision=0,
        outputs=tuple(
            OutputRuntimeState(
                bus_id=route.bus_id,
                base_scene_id=initial[route.bus_id] if initial[route.bus_id] in scene_ids else document.scenes[0].id,
                enabled=route.start_with_solin,
            )
            for route in document.outputs
        ),
    )
    state.validate_against(document)
    return state


class SceneRuntimeService:
    """Owns live output selection separately from editor undo/history."""

    def __init__(
        self,
        documents: SceneDocumentService,
        state: SceneRuntimeState,
        *,
        store: SceneRuntimeStore | None = None,
    ) -> None:
        state.validate_against(documents.document)
        self._documents = documents
        self._state = state
        self._store = store
        self._listeners: set[Callable[[SceneRuntimeState], None]] = set()
        self._unsubscribe_document = documents.subscribe(self._on_document_changed)

    @property
    def state(self) -> SceneRuntimeState:
        return self._state

    def subscribe(
        self,
        listener: Callable[[SceneRuntimeState], None],
    ) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def scene_allowed(self, bus_id: BusId, scene_id: str) -> bool:
        """Return whether ``scene_id`` may be routed to ``bus_id``."""
        return self._documents.document.scene(scene_id).allows_output(bus_id)

    def take_scene(self, bus_id: BusId, scene_id: str) -> SceneRuntimeState:
        """Route ONE output to ``scene_id``, leaving the others where they are."""
        self._require_allowed(bus_id, scene_id)
        current = self._state.output(bus_id)
        return self._commit(
            self._with_output(
                replace(current, mode=OutputMode.MANUAL, base_scene_id=scene_id)
            )
        )

    def select_scene(self, bus_id: BusId, scene_id: str) -> SceneRuntimeState:
        """Choose one output's return base and restore AUTO."""
        return self.select_base_scenes({bus_id: scene_id}, resume_automation=True)

    def select_base_scenes(
        self,
        selections: Mapping[BusId, str],
        *,
        resume_automation: bool = False,
    ) -> SceneRuntimeState:
        """Update valid output return bases atomically."""
        for bus_id, scene_id in selections.items():
            if bus_id not in DELIVERY_BUSES:
                raise SceneValidationError("Invalid runtime output bus")
            self._require_allowed(bus_id, scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.AUTO if resume_automation else output.mode,
                        base_scene_id=selections[output.bus_id],
                    ) if output.bus_id in selections else output
                    for output in self._state.outputs
                ),
            )
        )

    def take_program_scene(self, scene_id: str) -> SceneRuntimeState:
        """Pin every output the scene may use to ``scene_id`` in manual mode."""
        self._documents.document.scene(scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.MANUAL,
                        base_scene_id=scene_id,
                    )
                    if self.scene_allowed(output.bus_id, scene_id)
                    else output
                    for output in self._state.outputs
                ),
            )
        )

    def select_program_scene(self, scene_id: str) -> SceneRuntimeState:
        """Select every allowed output base without changing auto-switch.

        An output the scene is disabled for keeps its last scene.
        """

        return self.select_base_scenes({
            bus_id: scene_id
            for bus_id in DELIVERY_BUSES
            if self.scene_allowed(bus_id, scene_id)
        })

    def resume_program_automation(self) -> SceneRuntimeState:
        return self.set_program_automatic(
            True,
            scene_ids={output.bus_id: output.base_scene_id for output in self._state.outputs},
        )

    def set_program_automatic(
        self,
        enabled: bool,
        *,
        scene_ids: Mapping[BusId, str],
    ) -> SceneRuntimeState:
        """Commit all output modes and their selected bases or pins atomically."""
        if not isinstance(enabled, bool):
            raise SceneValidationError("Program automation state must be a boolean")
        if set(scene_ids) != set(DELIVERY_BUSES):
            raise SceneValidationError("Selected scenes must define every output bus")
        for scene_id in scene_ids.values():
            self._documents.document.scene(scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.AUTO if enabled else OutputMode.MANUAL,
                        base_scene_id=scene_ids[output.bus_id],
                    )
                    for output in self._state.outputs
                ),
            )
        )

    def set_output_enabled(self, bus_id: BusId, enabled: bool) -> SceneRuntimeState:
        if not isinstance(enabled, bool):
            raise SceneValidationError("Output enabled must be a boolean")
        current = self._state.output(bus_id)
        return self._commit(self._with_output(replace(current, enabled=enabled)))

    def resolve_scene(self, bus_id: BusId, category: ContentCategory) -> str:
        document = self._documents.document
        output = self._state.output(bus_id)
        scene_ids = {scene.id for scene in document.scenes}
        base_scene_id = output.base_scene_id if output.base_scene_id in scene_ids else document.scenes[0].id
        if output.mode is OutputMode.MANUAL:
            return base_scene_id
        if category not in AUTOMATIC_MEDIA_CATEGORIES:
            return base_scene_id
        automatic_scene_id = document.automation_for(bus_id).scene_for(category)
        # A media scene disabled for this output leaves it on its last scene.
        if automatic_scene_id is None or not document.scene(automatic_scene_id).allows_output(bus_id):
            return base_scene_id
        return automatic_scene_id

    def close(self) -> None:
        self._unsubscribe_document()

    def _on_document_changed(self, change: SceneDocumentChange) -> None:
        outputs = self._state.reconciled_against(change.document).outputs
        if outputs == self._state.outputs:
            return
        try:
            self._commit(replace(self._state, outputs=outputs))
        except Exception:  # noqa: BLE001 - persistence observer boundary
            log.warning("Could not reconcile scene runtime state", exc_info=True)

    def _require_allowed(self, bus_id: BusId, scene_id: str) -> None:
        if not self.scene_allowed(bus_id, scene_id):
            raise SceneValidationError(
                f"Scene is disabled for the {bus_id.value} output"
            )

    def _with_output(self, updated: OutputRuntimeState) -> SceneRuntimeState:
        return replace(
            self._state,
            outputs=tuple(
                updated if output.bus_id is updated.bus_id else output
                for output in self._state.outputs
            ),
        )

    def _commit(self, candidate: SceneRuntimeState) -> SceneRuntimeState:
        if replace(candidate, revision=self._state.revision) == self._state:
            return self._state
        updated = replace(candidate, revision=self._state.revision + 1)
        updated.validate_against(self._documents.document)
        if self._store is not None:
            self._store.save(updated, expected_revision=self._state.revision)
        self._state = updated
        for listener in tuple(self._listeners):
            try:
                listener(updated)
            except Exception:  # noqa: BLE001 - runtime observer boundary
                log.warning("Scene runtime listener failed", exc_info=True)
        return updated


def _strict_mapping(
    raw: object,
    *,
    field_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SceneValidationError(f"{field_name} must be an object")
    unknown = set(raw) - allowed_keys
    if unknown:
        raise SceneValidationError(f"Unknown {field_name} fields: {', '.join(sorted(unknown))}")
    return dict(raw)
