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
    mode: OutputMode = OutputMode.AUTO
    manual_scene_id: str = ""
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, BusId):
            raise SceneValidationError("Invalid runtime output bus")
        if not isinstance(self.mode, OutputMode):
            raise SceneValidationError("Invalid runtime output mode")
        if not isinstance(self.manual_scene_id, str) or len(self.manual_scene_id) > 128:
            raise SceneValidationError("Invalid runtime manual scene id")
        if self.mode is OutputMode.MANUAL and not self.manual_scene_id:
            raise SceneValidationError("Manual runtime mode requires a scene")
        if not isinstance(self.enabled, bool):
            raise SceneValidationError("Runtime output enabled must be a boolean")

    def to_record(self) -> dict[str, object]:
        return {
            "bus_id": self.bus_id.value,
            "mode": self.mode.value,
            "manual_scene_id": self.manual_scene_id,
            "enabled": self.enabled,
        }

    @classmethod
    def from_record(cls, raw: object) -> OutputRuntimeState:
        data = _strict_mapping(
            raw,
            field_name="runtime output",
            allowed_keys={"bus_id", "mode", "manual_scene_id", "enabled"},
        )
        try:
            bus_id = BusId(data.get("bus_id"))
            mode = OutputMode(data.get("mode", OutputMode.AUTO.value))
        except (TypeError, ValueError) as exc:
            raise SceneValidationError("Invalid runtime output enum") from exc
        manual_scene_id = data.get("manual_scene_id", "")
        enabled = data.get("enabled", False)
        if not isinstance(manual_scene_id, str) or not isinstance(enabled, bool):
            raise SceneValidationError("Invalid runtime output value")
        return cls(
            bus_id=bus_id,
            mode=mode,
            manual_scene_id=manual_scene_id,
            enabled=enabled,
        )


@dataclass(frozen=True, slots=True)
class SceneRuntimeState:
    document_id: str
    revision: int
    outputs: tuple[OutputRuntimeState, ...]
    schema_version: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id:
            raise SceneValidationError("Invalid runtime document id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise SceneValidationError("Invalid runtime revision")
        if self.schema_version != 3:
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
            if output.manual_scene_id and output.manual_scene_id not in scene_ids:
                raise SceneValidationError(
                    f"Runtime output {output.bus_id.value} references a missing scene"
                )

    def reconciled_against(self, document: SceneDocument) -> SceneRuntimeState:
        """Return a usable state after configuration changed while Solin was offline."""
        if self.document_id != document.document_id:
            return create_default_runtime_state(document)
        scene_ids = {scene.id for scene in document.scenes}
        outputs = tuple(
            (
                replace(output, mode=OutputMode.AUTO, manual_scene_id="")
                if output.manual_scene_id and output.manual_scene_id not in scene_ids
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
        if schema_version not in {1, 2, 3}:
            raise SceneValidationError("Unsupported runtime schema version")
        if (
            not isinstance(document_id, str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(outputs, list)
        ):
            raise SceneValidationError("Invalid scene runtime state")
        restored_outputs = tuple(OutputRuntimeState.from_record(item) for item in outputs)
        if schema_version == 1:
            try:
                primary = next(
                    output for output in restored_outputs if output.bus_id is BusId.VIRTUAL_CAMERA
                )
            except StopIteration as exc:
                raise SceneValidationError("Runtime state is missing the Program output") from exc
            legacy_selections = {
                (output.mode, output.manual_scene_id) for output in restored_outputs
            }
            program_mode = OutputMode.AUTO if len(legacy_selections) > 1 else primary.mode
            restored_outputs = tuple(
                replace(
                    output,
                    mode=program_mode,
                    manual_scene_id=primary.manual_scene_id,
                )
                for output in restored_outputs
            )
        return cls(
            document_id=document_id,
            revision=revision,
            outputs=restored_outputs,
            schema_version=3,
        )


def create_default_runtime_state(document: SceneDocument) -> SceneRuntimeState:
    return SceneRuntimeState(
        document_id=document.document_id,
        revision=0,
        outputs=tuple(
            OutputRuntimeState(
                bus_id=route.bus_id,
                enabled=route.start_with_solin,
            )
            for route in document.outputs
        ),
    )


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

    def take_scene(self, bus_id: BusId, scene_id: str) -> SceneRuntimeState:
        """Route ONE output to ``scene_id``, leaving the others where they are."""
        self._documents.document.scene(scene_id)
        current = self._state.output(bus_id)
        return self._commit(
            self._with_output(
                replace(current, mode=OutputMode.MANUAL, manual_scene_id=scene_id)
            )
        )

    def select_scene(self, bus_id: BusId, scene_id: str | None) -> SceneRuntimeState:
        """Choose one output's return base and restore AUTO; None clears the override."""
        return self.select_base_scenes({bus_id: scene_id}, resume_automation=True)

    def select_base_scenes(
        self,
        selections: Mapping[BusId, str | None],
        *,
        resume_automation: bool = False,
    ) -> SceneRuntimeState:
        """Update output return bases atomically; None uses the configured default."""
        for bus_id, scene_id in selections.items():
            if bus_id not in DELIVERY_BUSES:
                raise SceneValidationError("Invalid runtime output bus")
            if scene_id is not None:
                self._documents.document.scene(scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.AUTO if resume_automation else output.mode,
                        manual_scene_id=selections[output.bus_id] or "",
                    ) if output.bus_id in selections else output
                    for output in self._state.outputs
                ),
            )
        )

    def take_program_scene(self, scene_id: str) -> SceneRuntimeState:
        """Route every delivery output to ``scene_id`` (the old lockstep take).

        Still used where a single scene really is meant for everything — deleting
        the live scene, for instance.
        """
        self._documents.document.scene(scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.MANUAL,
                        manual_scene_id=scene_id,
                    )
                    for output in self._state.outputs
                ),
            )
        )

    def select_program_scene(self, scene_id: str | None) -> SceneRuntimeState:
        """Select the Program base, or clear its override, without changing auto-switch."""

        return self.select_base_scenes({bus_id: scene_id for bus_id in DELIVERY_BUSES})

    def resume_program_automation(self) -> SceneRuntimeState:
        return self.set_program_automatic(
            True,
            scene_ids={output.bus_id: output.manual_scene_id or None for output in self._state.outputs},
        )

    def set_program_automatic(
        self,
        enabled: bool,
        *,
        scene_ids: Mapping[BusId, str | None],
    ) -> SceneRuntimeState:
        """Commit all output modes and their selected bases or pins atomically."""
        if not isinstance(enabled, bool):
            raise SceneValidationError("Program automation state must be a boolean")
        if set(scene_ids) != set(DELIVERY_BUSES):
            raise SceneValidationError("Selected scenes must define every output bus")
        for scene_id in scene_ids.values():
            if scene_id is None and not enabled:
                raise SceneValidationError("Manual runtime mode requires a scene")
            if scene_id is not None:
                self._documents.document.scene(scene_id)
        return self._commit(
            replace(
                self._state,
                outputs=tuple(
                    replace(
                        output,
                        mode=OutputMode.AUTO if enabled else OutputMode.MANUAL,
                        manual_scene_id=scene_ids[output.bus_id] or "",
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
        if output.mode is OutputMode.MANUAL and output.manual_scene_id in scene_ids:
            return output.manual_scene_id
        route = document.output(bus_id)
        base_scene_id = (
            output.manual_scene_id
            if output.manual_scene_id in scene_ids
            else route.default_scene_id or document.scenes[0].id
        )
        if category not in AUTOMATIC_MEDIA_CATEGORIES:
            return base_scene_id
        return document.automation_for(bus_id).scene_for(category) or base_scene_id

    def close(self) -> None:
        self._unsubscribe_document()

    def _on_document_changed(self, change: SceneDocumentChange) -> None:
        scene_ids = {scene.id for scene in change.document.scenes}
        automation_configured = self._documents.program_automation_configured
        reconciled_outputs = []
        for output in self._state.outputs:
            manual_scene_id = (
                output.manual_scene_id
                if output.manual_scene_id in scene_ids
                else change.document.output(output.bus_id).default_scene_id
                or change.document.scenes[0].id
            )
            mode = (
                OutputMode.MANUAL
                if output.mode is OutputMode.AUTO and not automation_configured
                else output.mode
            )
            reconciled_outputs.append(replace(output, mode=mode, manual_scene_id=manual_scene_id))
        outputs = tuple(reconciled_outputs)
        if outputs == self._state.outputs:
            return
        try:
            self._commit(replace(self._state, outputs=outputs))
        except Exception:  # noqa: BLE001 - persistence observer boundary
            log.warning("Could not reconcile scene runtime state", exc_info=True)

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
