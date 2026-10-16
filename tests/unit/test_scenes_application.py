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
    CONTENT_SOURCE_ID,
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
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
)
from solin.core.scenes.engine import scene_engine_graph_signature
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
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
    assert all("default_scene_id" not in route.to_record() for route in service.document.outputs)

    service.delete_scene(CAMERA_SCENE_ID)

    assert all("default_scene_id" not in route.to_record() for route in service.document.outputs)


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

    service.set_program_media_scene(CONTENT_SCENE_ID)

    assert service.program_media_scene_id == CONTENT_SCENE_ID
    assert service.media_automation_configured
    for mapping in service.document.automation:
        assert mapping.scene_for(ContentCategory.IMAGE) == CONTENT_SCENE_ID
        assert mapping.scene_for(ContentCategory.VIDEO) == CONTENT_SCENE_ID


def test_media_scene_can_also_be_a_selected_base() -> None:
    service = _service()

    service.set_program_media_scene(CAMERA_SCENE_ID)
    assert service.program_media_scene_id == CAMERA_SCENE_ID


def test_program_roles_can_be_removed_independently() -> None:
    service = _service()

    service.set_program_media_scene(None)

    assert service.program_media_scene_id is None
    assert not service.media_automation_configured
    assert all(
        mapping.scene_for(ContentCategory.IMAGE) is None
        and mapping.scene_for(ContentCategory.VIDEO) is None
        for mapping in service.document.automation
    )

    assert SceneDocument.from_record(service.document.to_record()) == service.document


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


def test_source_and_occurrence_are_one_undoable_insertion() -> None:
    service = _service()
    before = service.document
    source = SourceDefinition(
        id="new-background", kind=SourceKind.COLOR, name="Shared background",
        configuration=ColorSourceConfig("#123456FF"),
    )
    layer = SceneLayer(id="new-occurrence", source_id=source.id, name="Background here")

    service.add_source_layer(CONTENT_SCENE_ID, source, layer)

    assert service.document.revision == before.revision + 1
    assert service.document.source(source.id) == source
    assert service.document.scene(CONTENT_SCENE_ID).layers[-1] == layer
    service.undo()
    assert service.document.sources == before.sources
    assert service.document.scenes == before.scenes
    assert not service.can_undo
    service.redo()
    assert service.document.source(source.id) == source
    assert service.document.scene(CONTENT_SCENE_ID).layers[-1] == layer


def test_rejected_scene_cycle_does_not_leave_an_orphaned_source_or_history() -> None:
    service = _service()
    before = service.document
    source = SourceDefinition(
        id="self-reference", kind=SourceKind.SCENE_REFERENCE, name="Self",
        configuration=SceneReferenceConfig(CONTENT_SCENE_ID),
    )
    layer = SceneLayer(id="self-layer", source_id=source.id, name="Self")

    with pytest.raises(SceneValidationError):
        service.add_source_layer(CONTENT_SCENE_ID, source, layer)

    assert service.document is before
    assert not service.can_undo
    assert not service.can_redo


def test_failed_source_insertion_does_not_publish_partial_document_or_history() -> None:
    class RejectingStore:
        def save(self, document, *, expected_revision=None):
            raise OSError("disk full")

    service = SceneDocumentService(_document(), store=RejectingStore())
    before = service.document
    changes = []
    unsubscribe = service.subscribe(changes.append)
    source = SourceDefinition(
        id="new-background", kind=SourceKind.COLOR, name="Background",
        configuration=ColorSourceConfig("#123456FF"),
    )
    try:
        with pytest.raises(OSError, match="disk full"):
            service.add_source_layer(
                CONTENT_SCENE_ID, source,
                SceneLayer(id="new-layer", source_id=source.id, name="Background"),
            )
    finally:
        unsubscribe()

    assert service.document is before
    assert changes == []
    assert not service.can_undo
    assert not service.can_redo


def test_rename_changes_only_one_occurrence_and_supports_undo_redo() -> None:
    service = _service()
    source = service.document.source(CONTENT_SOURCE_ID)
    first = service.document.scene(CONTENT_SCENE_ID).layers[0]
    second = SceneLayer(id="second-content", source_id=source.id, name="Second content")
    service.add_source_layer(CONTENT_SCENE_ID, source, second)
    before = service.document

    service.rename_layer(CONTENT_SCENE_ID, first.id, "  Custom content  ")

    layers = service.document.scene(CONTENT_SCENE_ID).layers
    assert layers[0] == replace(first, name="Custom content")
    assert layers[-1] == second
    assert service.document.sources == before.sources
    assert service.document.scene(CONTENT_SCENE_ID).layers[0].rect == first.rect
    assert service.document.source(source.id).name == source.name
    assert scene_engine_graph_signature(service.document) == scene_engine_graph_signature(before)
    service.undo()
    assert service.document.scene(CONTENT_SCENE_ID).layers == before.scene(CONTENT_SCENE_ID).layers
    service.redo()
    assert service.document.scene(CONTENT_SCENE_ID).layers[0].name == "Custom content"


def test_layer_rename_does_not_hide_structural_changes_from_graph_signature() -> None:
    service = _service()
    scene = service.document.scene(CONTENT_SCENE_ID)
    layer = scene.layers[0]
    before = scene_engine_graph_signature(service.document)

    service.update_layer(CONTENT_SCENE_ID, layer.id, replace(layer, name="Renamed", visible=False))

    assert scene_engine_graph_signature(service.document) != before


@pytest.mark.parametrize("name", ["", "  "])
def test_invalid_layer_rename_preserves_document_and_history(name: str) -> None:
    service = _service()
    before = service.document
    layer = before.scene(CONTENT_SCENE_ID).layers[0]

    with pytest.raises(SceneValidationError, match="layer name"):
        service.rename_layer(CONTENT_SCENE_ID, layer.id, name)

    assert service.document is before
    assert not service.can_undo


def test_renaming_a_layer_to_its_current_name_does_not_create_history() -> None:
    service = _service()
    before = service.document
    layer = before.scene(CONTENT_SCENE_ID).layers[0]

    service.rename_layer(CONTENT_SCENE_ID, layer.id, layer.name)

    assert service.document is before
    assert not service.can_undo


@pytest.mark.parametrize("operation", ["add", "source", "rename", "draft"])
@pytest.mark.parametrize("proposed", ["Content", "  content  ", "CONTENT"])
def test_duplicate_layer_names_are_rejected_without_persistence_or_history(operation, proposed) -> None:
    class Store:
        def __init__(self):
            self.writes = []

        def save(self, document, *, expected_revision=None):
            self.writes.append(document)

    store = Store()
    service = SceneDocumentService(_document(), store=store)
    source = service.document.source(CONTENT_SOURCE_ID)
    existing = service.document.scene(CONTENT_SCENE_ID).layers[0]
    second = SceneLayer(id="second", source_id=source.id, name="Detail")
    if operation in {"rename", "draft"}:
        service.add_layer(CONTENT_SCENE_ID, second)
    before = service.document
    writes_before = len(store.writes)
    changes = []
    service.subscribe(changes.append)

    with pytest.raises(SceneConflictError, match="name"):
        if operation == "add":
            service.add_layer(CONTENT_SCENE_ID, replace(second, name=proposed))
        elif operation == "source":
            service.add_source_layer(CONTENT_SCENE_ID, source, replace(second, name=proposed))
        elif operation == "rename":
            service.rename_layer(CONTENT_SCENE_ID, second.id, proposed)
        else:
            draft = service.begin_scene_edit(CONTENT_SCENE_ID)
            draft.update_layer(second.id, replace(second, name=proposed))
            service.commit_scene_edit(draft)

    assert service.document is before
    assert len(store.writes) == writes_before
    assert changes == []
    assert service.document.scene(CONTENT_SCENE_ID).layers[0] == existing
    if operation in {"rename", "draft"}:
        service.undo()
        assert service.document.scene(CONTENT_SCENE_ID).layers == (existing,)
        assert not service.can_undo
    else:
        assert not service.can_undo


def test_layer_names_are_unique_only_within_their_scene_and_exclude_the_renamed_layer() -> None:
    service = _service()
    content = service.document.scene(CONTENT_SCENE_ID).layers[0]
    camera = service.document.scene(CAMERA_SCENE_ID).layers[0]

    service.rename_layer(CAMERA_SCENE_ID, camera.id, content.name)
    service.validate_layer_name(CAMERA_SCENE_ID, content.name, excluding_layer_id=camera.id)
    service.rename_layer(CAMERA_SCENE_ID, camera.id, content.name.upper())
    assert service.document.scene(CAMERA_SCENE_ID).layers[0].name == content.name.upper()
    service.undo()
    assert service.document.scene(CAMERA_SCENE_ID).layers[0].name == content.name
    service.redo()
    assert service.document.scene(CAMERA_SCENE_ID).layers[0].name == content.name.upper()


def test_historical_duplicate_names_allow_other_edits_and_can_be_renamed_away() -> None:
    document = _document()
    scene = document.scene(CONTENT_SCENE_ID)
    original = scene.layers[0]
    duplicate = replace(original, id="historical-duplicate")
    legacy_scene = replace(scene, layers=(original, duplicate))
    service = SceneDocumentService(replace(document, scenes=tuple(
        legacy_scene if item.id == scene.id else item for item in document.scenes
    )))
    before = service.document

    service.rename_layer(scene.id, original.id, original.name)
    assert service.document is before
    service.update_layer(scene.id, original.id, replace(original, visible=False))
    service.rename_layer(scene.id, duplicate.id, "Other content")
    assert [layer.name for layer in service.document.scene(scene.id).layers] == [original.name, "Other content"]
    service.undo()
    assert service.document.scene(scene.id).layers[1].name == original.name
