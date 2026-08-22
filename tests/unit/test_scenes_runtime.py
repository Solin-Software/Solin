from __future__ import annotations

from dataclasses import replace
import threading

from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.model import BusId, ContentCategory, OutputMode, SceneDocument
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import (
    SceneRuntimePersistenceQueue,
    SceneRuntimeRepository,
)
from solin.core.scenes.runtime import (
    SceneRuntimeService,
    SceneRuntimeState,
    create_default_runtime_state,
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


def _services() -> tuple[SceneDocumentService, SceneRuntimeService]:
    documents = SceneDocumentService(_document())
    runtime = SceneRuntimeService(
        documents,
        create_default_runtime_state(documents.document),
    )
    return documents, runtime


def test_manual_scene_is_sticky_until_automation_is_resumed() -> None:
    documents, runtime = _services()
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IMAGE) == CONTENT_SCENE_ID

    runtime.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)
    assert all(output.mode is OutputMode.MANUAL for output in runtime.state.outputs)
    assert (
        runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.VIDEO)
        == CONTENT_CAMERA_PIP_SCENE_ID
    )

    runtime.resume_program_automation()
    assert all(output.mode is OutputMode.AUTO for output in runtime.state.outputs)
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IMAGE) == CONTENT_SCENE_ID


def test_enabling_automation_can_normalize_a_media_scene_base() -> None:
    _documents, runtime = _services()
    runtime.take_program_scene(CONTENT_SCENE_ID)

    runtime.set_program_automatic(
        True,
        current_scene_id=CONTENT_SCENE_ID,
        automatic_base_scene_id=CAMERA_SCENE_ID,
    )

    assert all(output.mode is OutputMode.AUTO for output in runtime.state.outputs)
    assert {output.manual_scene_id for output in runtime.state.outputs} == {CAMERA_SCENE_ID}
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IDLE) == CAMERA_SCENE_ID
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.VIDEO) == CONTENT_SCENE_ID


def test_live_take_is_not_part_of_editor_undo_history() -> None:
    documents, runtime = _services()
    documents.rename_scene(CONTENT_SCENE_ID, "Renamed")
    runtime.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    documents.undo()

    assert documents.document.scene(CONTENT_SCENE_ID).name == "Content"
    output = runtime.state.output(BusId.VIRTUAL_CAMERA)
    assert output.mode is OutputMode.MANUAL
    assert output.manual_scene_id == CONTENT_CAMERA_PIP_SCENE_ID


def test_deleting_a_selected_scene_fails_safe_to_document_fallback() -> None:
    documents, runtime = _services()
    runtime.take_program_scene(CONTENT_SCENE_ID)

    documents.delete_scene(CONTENT_SCENE_ID)

    output = runtime.state.output(BusId.MEDIA_WINDOWS)
    assert output.mode is OutputMode.MANUAL
    assert output.manual_scene_id == CAMERA_SCENE_ID
    assert runtime.resolve_scene(BusId.MEDIA_WINDOWS, ContentCategory.IMAGE) == CAMERA_SCENE_ID


def test_removing_a_program_role_disables_invalid_automation() -> None:
    documents, runtime = _services()
    runtime.select_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    documents.set_program_media_scene(None)

    assert all(output.mode is OutputMode.MANUAL for output in runtime.state.outputs)
    assert {
        output.manual_scene_id for output in runtime.state.outputs
    } == {CONTENT_CAMERA_PIP_SCENE_ID}
    assert (
        runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IMAGE)
        == CONTENT_CAMERA_PIP_SCENE_ID
    )


def test_runtime_repository_persists_only_small_live_state(tmp_path) -> None:
    documents = SceneDocumentService(_document())
    repository = SceneRuntimeRepository(tmp_path / "scenes_runtime.json")
    initial = repository.load_or_create(documents.document)
    runtime = SceneRuntimeService(documents, initial, store=repository)

    updated = runtime.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)
    runtime.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    reloaded = repository.load_or_create(documents.document)
    assert reloaded.revision == updated.revision + 1
    assert reloaded.output(BusId.VIRTUAL_CAMERA).enabled
    assert reloaded.output(BusId.VIRTUAL_CAMERA).manual_scene_id == (CONTENT_CAMERA_PIP_SCENE_ID)
    assert repository.path.stat().st_size < 4096


def test_runtime_persistence_queue_coalesces_without_blocking_the_caller() -> None:
    class _BlockingRepository:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.saved: list[SceneRuntimeState] = []

        def save(self, state, *, expected_revision=None) -> None:
            assert expected_revision == (self.saved[-1].revision if self.saved else 0)
            self.started.set()
            assert self.release.wait(2)
            self.saved.append(state)

    initial = create_default_runtime_state(_document())
    repository = _BlockingRepository()
    queue = SceneRuntimePersistenceQueue(repository, initial)
    first = replace(initial, revision=1)
    latest = replace(initial, revision=2)

    queue.save(first, expected_revision=0)
    assert repository.started.wait(1)
    queue.save(latest, expected_revision=1)
    assert repository.saved == []

    repository.release.set()
    assert queue.flush()
    assert repository.saved[-1] == latest
    assert queue.close()


def test_runtime_persistence_queue_retries_a_transient_storage_failure() -> None:
    class _FlakyRepository:
        def __init__(self) -> None:
            self.attempts = 0
            self.saved: SceneRuntimeState | None = None

        def save(self, state, *, expected_revision=None) -> None:
            assert expected_revision == 0
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary storage failure")
            self.saved = state

    initial = create_default_runtime_state(_document())
    repository = _FlakyRepository()
    queue = SceneRuntimePersistenceQueue(
        repository,
        initial,
        retry_delay_seconds=0.001,
    )
    updated = replace(initial, revision=1)

    queue.save(updated, expected_revision=0)

    assert queue.flush()
    assert repository.attempts == 2
    assert repository.saved == updated
    assert queue.last_error is None
    assert queue.close()


def test_runtime_repository_recovers_a_stale_manual_scene_after_offline_edit(
    tmp_path,
) -> None:
    document = _document()
    repository = SceneRuntimeRepository(tmp_path / "scenes_runtime.json")
    documents = SceneDocumentService(document)
    runtime = SceneRuntimeService(
        documents,
        repository.load_or_create(document),
        store=repository,
    )
    runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
    stale = runtime.take_program_scene(CONTENT_SCENE_ID)
    edited = SceneDocumentService(document).delete_scene(CONTENT_SCENE_ID)

    recovered = repository.load_or_create(edited)

    output = recovered.output(BusId.MEDIA_WINDOWS)
    assert recovered.revision == stale.revision + 1
    assert output.mode is OutputMode.AUTO
    assert output.manual_scene_id == ""
    assert output.enabled


def test_runtime_schema_one_migrates_divergent_destinations_to_one_auto_program() -> None:
    document = _document()
    state = create_default_runtime_state(document).to_record()
    state["schema_version"] = 1
    media, virtual = state["outputs"]
    media.update(mode=OutputMode.MANUAL.value, manual_scene_id=CONTENT_SCENE_ID)
    virtual.update(mode=OutputMode.MANUAL.value, manual_scene_id=CAMERA_SCENE_ID)

    restored = SceneRuntimeService(
        SceneDocumentService(document),
        SceneRuntimeState.from_record(state),
    ).state

    assert restored.schema_version == 2
    assert all(output.mode is OutputMode.AUTO for output in restored.outputs)
    assert {
        output.manual_scene_id for output in restored.outputs
    } == {CAMERA_SCENE_ID}
