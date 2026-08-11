from __future__ import annotations

from dataclasses import replace

import pytest

from solin.core.scenes.application import (
    SceneConflictError,
    SceneDocumentService,
    SceneHistoryEmptyError,
    SceneNotFoundError,
)
from solin.core.scenes.model import (
    DEFAULT_CAMERA_SOURCE_ID,
    CameraPreset,
    ColorSourceConfig,
    ContentCategory,
    OnvifPtzBinding,
    PtzPosition,
    RecallPtzPresetAction,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)


def _document() -> SceneDocument:
    return create_default_scene_document(
        SceneSeedNames(
            content_source="Current content",
            default_camera_source="Default camera",
            no_signal_source="No signal background",
            content_scene="Content",
            camera_scene="Camera",
            content_camera_pip_scene="Content + camera",
            no_signal_scene="No signal",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
        document_id="test-scene-document",
        created_at="2026-08-02T12:00:00+00:00",
    )


def _service(*, identities: list[str] | None = None) -> SceneDocumentService:
    generated = iter(identities or ["generated-id"])
    return SceneDocumentService(
        _document(),
        clock=lambda: "2026-08-02T13:00:00+00:00",
        identity_factory=lambda: next(generated),
    )


def test_scene_crud_is_transactional_and_revisions_are_monotonic() -> None:
    service = _service(identities=["new-scene", "duplicate-scene", "duplicate-layer"])

    service.create_scene("Custom")
    assert service.document.revision == 1
    assert service.document.scene("new-scene").name == "Custom"

    service.rename_scene("new-scene", "Custom renamed")
    service.reorder_scene("new-scene", 0)
    service.duplicate_scene(CONTENT_SCENE_ID, name="Content copy")

    assert service.document.revision == 4
    assert service.document.scenes[0].id == "new-scene"
    duplicate = service.document.scene("duplicate-scene")
    assert duplicate.name == "Content copy"
    assert duplicate.layers[0].id == "duplicate-layer"
    assert (
        duplicate.layers[0].source_id
        == service.document.scene(CONTENT_SCENE_ID).layers[0].source_id
    )

    service.delete_scene("new-scene")
    assert service.document.revision == 5
    assert all(scene.id != "new-scene" for scene in service.document.scenes)


def test_program_and_scene_transition_policy_support_undo_and_redo() -> None:
    service = _service()
    profile_spec = TransitionSpec(TransitionKind.FADE_TO_BLACK, 700)
    scene_spec = TransitionSpec(TransitionKind.DISSOLVE, 450)

    service.set_program_transition(profile_spec)
    service.set_scene_transition_override(CONTENT_SCENE_ID, scene_spec)

    assert service.effective_transition(CAMERA_SCENE_ID) == profile_spec
    assert service.effective_transition(CONTENT_SCENE_ID) == scene_spec
    assert service.document.revision == 2

    service.undo()
    assert service.effective_transition(CONTENT_SCENE_ID) == profile_spec
    service.undo()
    assert service.effective_transition(CONTENT_SCENE_ID) == TransitionSpec(
        TransitionKind.DISSOLVE,
        350,
    )
    service.redo()
    service.redo()
    assert service.effective_transition(CONTENT_SCENE_ID) == scene_spec


def test_duplicate_scene_copies_override_and_delete_scene_removes_it() -> None:
    service = _service(identities=["duplicate-scene", "duplicate-layer"])
    override = TransitionSpec(TransitionKind.FADE_TO_BLACK, 500)
    service.set_scene_transition_override(CONTENT_SCENE_ID, override)

    service.duplicate_scene(CONTENT_SCENE_ID, name="Content copy")

    assert service.document.transition_policy.override_for("duplicate-scene") == override
    service.delete_scene("duplicate-scene")
    assert service.document.transition_policy.override_for("duplicate-scene") is None


def test_scene_transition_override_can_be_removed_and_unknown_scene_is_rejected() -> None:
    service = _service()
    override = TransitionSpec(TransitionKind.CUT, 0)

    service.set_scene_transition_override(CONTENT_SCENE_ID, override)
    assert service.effective_transition(CONTENT_SCENE_ID) == override
    service.set_scene_transition_override(CONTENT_SCENE_ID, None)
    assert service.effective_transition(CONTENT_SCENE_ID) == (
        service.document.transition_policy.default
    )

    with pytest.raises(SceneNotFoundError, match="does not exist"):
        service.set_scene_transition_override("missing-scene", override)
    with pytest.raises(SceneNotFoundError, match="does not exist"):
        service.effective_transition("missing-scene")


def test_referenced_scene_deletion_clears_program_roles_transactionally() -> None:
    service = _service()

    service.delete_scene(CONTENT_SCENE_ID)

    assert all(scene.id != CONTENT_SCENE_ID for scene in service.document.scenes)
    assert all(not mapping.assignments for mapping in service.document.automation)
    assert all(
        route.default_scene_id == CAMERA_SCENE_ID for route in service.document.outputs
    )

    service.delete_scene(CAMERA_SCENE_ID)

    assert all(not route.default_scene_id for route in service.document.outputs)


def test_scene_deletion_cascades_scene_reference_sources_transactionally() -> None:
    service = _service()
    reference = SourceDefinition(
        id="content-scene-reference",
        kind=SourceKind.SCENE_REFERENCE,
        name="Content composition",
        configuration=SceneReferenceConfig(CONTENT_SCENE_ID),
    )
    service.create_source(reference)
    service.add_layer(
        CAMERA_SCENE_ID,
        SceneLayer(
            id="content-scene-layer",
            source_id=reference.id,
            name="Content composition",
        ),
    )

    service.delete_scene(CONTENT_SCENE_ID)

    assert all(source.id != reference.id for source in service.document.sources)
    assert all(
        layer.source_id != reference.id
        for scene in service.document.scenes
        for layer in scene.layers
    )


def test_source_cascade_removes_layers_presets_and_scene_entry_actions() -> None:
    service = _service()
    camera = service.document.source(DEFAULT_CAMERA_SOURCE_ID)
    service.update_source(
        camera.id,
        replace(
            camera,
            configuration=replace(
                camera.configuration,
                ptz_binding=OnvifPtzBinding(endpoint="https://camera.local/onvif/device_service"),
            ),
        ),
    )
    preset = CameraPreset(
        id="camera-preset",
        camera_source_id=DEFAULT_CAMERA_SOURCE_ID,
        name="Lectern",
        position=PtzPosition(pan=0.1, tilt=-0.2, zoom=0.5),
        created_at="2026-08-02T12:00:00+00:00",
        updated_at="2026-08-02T12:00:00+00:00",
    )
    service.create_camera_preset(preset)
    service.set_scene_entry_action(
        CAMERA_SCENE_ID,
        RecallPtzPresetAction(preset_id=preset.id),
    )

    with pytest.raises(SceneConflictError, match="requires cascade"):
        service.delete_source(DEFAULT_CAMERA_SOURCE_ID)

    service.delete_source(DEFAULT_CAMERA_SOURCE_ID, cascade=True)

    assert all(source.id != DEFAULT_CAMERA_SOURCE_ID for source in service.document.sources)
    assert service.document.camera_presets == ()
    assert all(
        layer.source_id != DEFAULT_CAMERA_SOURCE_ID
        for scene in service.document.scenes
        for layer in scene.layers
    )
    assert all(not scene.entry_actions for scene in service.document.scenes)


def test_layer_crud_preserves_source_references_and_z_order() -> None:
    service = _service()
    source = SourceDefinition(
        id="brand-background",
        kind=SourceKind.COLOR,
        name="Brand background",
        configuration=ColorSourceConfig("#123456FF"),
    )
    layer = SceneLayer(
        id="brand-layer",
        source_id=source.id,
        name="Brand",
    )
    service.create_source(source)
    service.add_layer(CONTENT_SCENE_ID, layer, index=0)
    assert service.document.scene(CONTENT_SCENE_ID).layers[0] == layer

    service.update_layer(
        CONTENT_SCENE_ID,
        layer.id,
        replace(layer, opacity=0.5),
    )
    service.reorder_layer(CONTENT_SCENE_ID, layer.id, 1)
    updated_layers = service.document.scene(CONTENT_SCENE_ID).layers
    assert updated_layers[1].id == layer.id
    assert updated_layers[1].opacity == 0.5

    service.delete_layer(CONTENT_SCENE_ID, layer.id)
    service.delete_source(source.id)
    assert all(candidate.id != source.id for candidate in service.document.sources)


def test_removing_the_last_layer_prunes_its_private_source_atomically() -> None:
    service = _service()
    source = SourceDefinition(
        id="temporary-overlay",
        kind=SourceKind.COLOR,
        name="Temporary overlay",
        configuration=ColorSourceConfig("#123456FF"),
    )
    first = SceneLayer(id="temporary-first", source_id=source.id, name=source.name)
    second = SceneLayer(id="temporary-second", source_id=source.id, name=source.name)
    service.create_source(source)
    service.add_layer(CONTENT_SCENE_ID, first)
    service.add_layer(CAMERA_SCENE_ID, second)

    service.delete_layer_and_orphaned_source(CONTENT_SCENE_ID, first.id)

    assert service.document.source(source.id) == source
    before_last_delete = service.document.revision

    service.delete_layer_and_orphaned_source(CAMERA_SCENE_ID, second.id)

    assert service.document.revision == before_last_delete + 1
    assert all(candidate.id != source.id for candidate in service.document.sources)


def test_undo_and_redo_restore_content_without_reusing_old_revisions() -> None:
    service = _service()
    original_name = service.document.scene(CONTENT_SCENE_ID).name
    service.rename_scene(CONTENT_SCENE_ID, "Renamed")
    assert service.document.revision == 1

    service.undo()
    assert service.document.revision == 2
    assert service.document.scene(CONTENT_SCENE_ID).name == original_name

    service.redo()
    assert service.document.revision == 3
    assert service.document.scene(CONTENT_SCENE_ID).name == "Renamed"

    service.undo()
    service.rename_scene(CONTENT_SCENE_ID, "Different branch")
    assert not service.can_redo
    with pytest.raises(SceneHistoryEmptyError):
        service.redo()


def test_scene_draft_coalesces_many_preview_updates_into_one_commit() -> None:
    service = _service()
    draft = service.begin_scene_edit(CONTENT_SCENE_ID)
    layer = draft.scene.layers[0]

    for step in range(1, 61):
        draft.update_layer(
            layer.id,
            replace(layer, opacity=step / 60),
        )

    assert service.document.revision == 0
    service.commit_scene_edit(draft)
    assert service.document.revision == 1
    assert service.document.scene(CONTENT_SCENE_ID).layers[0].opacity == 1.0


def test_reapplying_the_same_program_media_scene_is_a_noop() -> None:
    service = _service()
    before = service.document

    after = service.set_program_media_scene(CONTENT_SCENE_ID)

    assert after is before
    assert after.revision == 0


def test_program_roles_are_updated_for_both_destinations_atomically() -> None:
    service = _service()

    service.set_program_default_scene(CAMERA_SCENE_ID)
    service.set_program_media_scene(CONTENT_SCENE_ID)

    assert service.program_default_scene_id == CAMERA_SCENE_ID
    assert service.program_media_scene_id == CONTENT_SCENE_ID
    assert service.program_automation_configured
    assert {route.default_scene_id for route in service.document.outputs} == {CAMERA_SCENE_ID}
    for mapping in service.document.automation:
        assert mapping.scene_for(ContentCategory.IMAGE) == CONTENT_SCENE_ID
        assert mapping.scene_for(ContentCategory.VIDEO) == CONTENT_SCENE_ID


def test_program_roles_must_be_different() -> None:
    service = _service()

    with pytest.raises(SceneConflictError, match="must be different"):
        service.set_program_media_scene(CAMERA_SCENE_ID)


def test_direct_content_scene_is_independent_from_the_program_media_role() -> None:
    service = _service()

    service.set_program_media_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    assert service.program_media_scene_id == CONTENT_CAMERA_PIP_SCENE_ID
    assert service.direct_content_scene_id == CONTENT_SCENE_ID

    content_scene = service.document.scene(CONTENT_SCENE_ID)
    content_layer = content_scene.layers[0]
    service.update_scene(
        CONTENT_SCENE_ID,
        replace(
            content_scene,
            layers=(replace(content_layer, opacity=0.8),),
        ),
    )

    assert service.direct_content_scene_id is None


def test_program_roles_can_be_removed_independently() -> None:
    service = _service()

    service.set_program_media_scene(None)

    assert service.program_default_scene_id == CAMERA_SCENE_ID
    assert service.program_media_scene_id is None
    assert not service.program_automation_configured
    assert all(
        mapping.scene_for(ContentCategory.IMAGE) is None
        and mapping.scene_for(ContentCategory.VIDEO) is None
        for mapping in service.document.automation
    )

    service.set_program_default_scene(None)

    assert service.program_default_scene_id is None
    assert {route.default_scene_id for route in service.document.outputs} == {""}
    restored = SceneDocument.from_record(service.document.to_record())
    assert {route.default_scene_id for route in restored.outputs} == {""}


def test_failed_persistence_leaves_document_and_history_unchanged() -> None:
    attempts: list[tuple[int, int]] = []

    class _RejectingStore:
        def save(
            self,
            document: SceneDocument,
            *,
            expected_revision: int | None = None,
        ) -> None:
            assert expected_revision is not None
            attempts.append((document.revision, expected_revision))
            raise OSError("disk full")

    service = SceneDocumentService(
        _document(),
        store=_RejectingStore(),
    )

    with pytest.raises(OSError, match="disk full"):
        service.rename_scene(CONTENT_SCENE_ID, "Cannot persist")

    assert attempts == [(1, 0)]
    assert service.document.revision == 0
    assert service.document.scene(CONTENT_SCENE_ID).name == "Content"
    assert not service.can_undo
    assert not service.can_redo
