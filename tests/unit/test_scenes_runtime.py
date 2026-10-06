from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from solin.core.scenes import repository as scene_repository
from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.model import BusId, ContentCategory, OutputMode, SceneDocument, SceneValidationError
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import (
    SceneRepositoryCorruptError,
    SceneRepositoryError,
    SceneRevisionConflictError,
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
        scene_ids={output.bus_id: CAMERA_SCENE_ID for output in runtime.state.outputs},
    )

    assert all(output.mode is OutputMode.AUTO for output in runtime.state.outputs)
    assert {output.manual_scene_id for output in runtime.state.outputs} == {CAMERA_SCENE_ID}
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IDLE) == CAMERA_SCENE_ID
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.VIDEO) == CONTENT_SCENE_ID


def test_automatic_program_base_can_return_to_the_configured_default() -> None:
    _documents, runtime = _services()
    runtime.select_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)

    runtime.select_program_scene(None)

    assert all(output.manual_scene_id == "" for output in runtime.state.outputs)
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IDLE) == CAMERA_SCENE_ID


def test_base_selection_batch_validates_before_mutating_any_output() -> None:
    _documents, runtime = _services()
    before = runtime.state
    with pytest.raises(StopIteration):
        runtime.select_base_scenes({
            BusId.MEDIA_WINDOWS: CONTENT_CAMERA_PIP_SCENE_ID,
            BusId.VIRTUAL_CAMERA: "missing-scene",
        })
    assert runtime.state is before
    with pytest.raises(SceneValidationError):
        runtime.select_base_scenes({BusId.EDITOR: CAMERA_SCENE_ID})
    assert runtime.state is before


def test_automation_disable_requires_valid_current_scenes_for_every_output() -> None:
    _documents, runtime = _services()
    before = runtime.state
    with pytest.raises(SceneValidationError):
        runtime.set_program_automatic(False, scene_ids={BusId.MEDIA_WINDOWS: CAMERA_SCENE_ID})
    assert runtime.state is before
    with pytest.raises(StopIteration):
        runtime.set_program_automatic(False, scene_ids={
            BusId.MEDIA_WINDOWS: CAMERA_SCENE_ID,
            BusId.VIRTUAL_CAMERA: "missing-scene",
        })
    assert runtime.state is before


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


class _GatedRuntimeRepository:
    def __init__(self, failure: Exception | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.saved: list[SceneRuntimeState] = []
        self.attempts = 0
        self.failure = failure

    def save(self, state, *, expected_revision=None) -> None:
        assert expected_revision == (self.saved[-1].revision if self.saved else 0)
        self.attempts += 1
        self.started.set()
        assert self.release.wait(5)
        if self.failure is not None:
            raise self.failure
        self.saved.append(state)


def test_runtime_persistence_queue_coalesces_without_blocking_the_caller() -> None:
    initial = create_default_runtime_state(_document())
    repository = _GatedRuntimeRepository()
    queue = SceneRuntimePersistenceQueue(repository, initial)
    first = replace(initial, revision=1)
    latest = replace(initial, revision=2)

    try:
        queue.save(first, expected_revision=0)
        assert repository.started.wait(1)
        queue.save(latest, expected_revision=1)
        assert repository.saved == []

        repository.release.set()
        assert queue.flush()
        assert repository.saved[-1] == latest
        assert queue.close()
    finally:
        repository.release.set()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


@pytest.mark.parametrize("drain", ["flush", "close"])
def test_runtime_persistence_queue_retries_a_transient_storage_failure(drain) -> None:
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

    try:
        queue.save(updated, expected_revision=0)

        assert getattr(queue, drain)()
        assert repository.attempts == 2
        assert repository.saved == updated
        assert queue.last_error is None
        assert queue.close()
    finally:
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


@pytest.mark.parametrize("external_change", ["deleted", "revision", "corrupt"])
def test_runtime_persistence_queue_stops_on_permanent_storage_failure(
    tmp_path, external_change
) -> None:
    class _CountingRepository(SceneRuntimeRepository):
        attempts = 0

        def save(self, state, *, expected_revision=None) -> None:
            self.attempts += 1
            super().save(state, expected_revision=expected_revision)

    document = _document()
    repository = _CountingRepository(tmp_path / "scenes_runtime.json")
    initial = replace(repository.load_or_create(document), revision=1)
    repository.save(initial, expected_revision=0)
    repository.attempts = 0
    queue = SceneRuntimePersistenceQueue(repository, initial, retry_delay_seconds=0.001)
    documents = SceneDocumentService(document)
    runtime = SceneRuntimeService(documents, initial, store=queue)
    try:
        if external_change == "deleted":
            repository.path.unlink()
            error_type = SceneRevisionConflictError
            expected_bytes = None
        elif external_change == "revision":
            repository.save(replace(initial, revision=2), expected_revision=1)
            repository.attempts = 0
            error_type = SceneRevisionConflictError
            expected_bytes = repository.path.read_bytes()
        else:
            expected_bytes = b"invalid runtime JSON"
            repository.path.write_bytes(expected_bytes)
            error_type = SceneRepositoryCorruptError

        accepted = runtime.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)
        with queue._condition:
            assert queue._condition.wait_for(lambda: queue.last_error is not None, timeout=1)
        error = queue.last_error
        assert isinstance(error, error_type)

        started = time.monotonic()
        assert not queue.flush(timeout_seconds=1)
        assert time.monotonic() - started < 0.3
        with pytest.raises(SceneRepositoryError) as rejected:
            runtime.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
        assert rejected.value.__cause__ is error
        assert runtime.state is accepted
        with pytest.raises(SceneRepositoryError):
            queue.save(accepted, expected_revision=accepted.revision)

        started = time.monotonic()
        assert not queue.close(timeout_seconds=1)
        assert time.monotonic() - started < 0.3
        assert not queue._thread.is_alive()
        assert not queue.close(timeout_seconds=0)
        assert queue.last_error is error
        assert repository.attempts == 1
        if expected_bytes is None:
            assert not repository.path.exists()
        else:
            assert repository.path.read_bytes() == expected_bytes
    finally:
        runtime.close()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unexpected failure"),
        ValueError("invalid state"),
        SceneRevisionConflictError("external revision"),
        SceneRepositoryCorruptError("runtime.json"),
    ],
)
def test_runtime_persistence_queue_preserves_latest_state_after_terminal_failure(error) -> None:
    initial = create_default_runtime_state(_document())
    repository = _GatedRuntimeRepository(error)
    queue = SceneRuntimePersistenceQueue(repository, initial, retry_delay_seconds=0.001)
    latest = replace(initial, revision=2)
    try:
        queue.save(replace(initial, revision=1), expected_revision=0)
        assert repository.started.wait(1)
        queue.save(latest, expected_revision=1)
        repository.release.set()
        assert not queue.flush(1)
        assert not queue.close(1)
        assert not queue._thread.is_alive()
        assert queue.last_error is error
        assert queue._accepted_state is latest
        assert queue._pending is latest
        assert queue._persisted_revision == 0
        assert not queue._inflight
        assert repository.attempts == 1
    finally:
        repository.release.set()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


def test_runtime_persistence_queue_recovers_wrapped_read_failure_and_coalesces(
    tmp_path, monkeypatch
) -> None:
    document = _document()
    repository = SceneRuntimeRepository(tmp_path / "scenes_runtime.json")
    initial = repository.load_or_create(document)
    read_record = scene_repository._read_json_record
    started = threading.Event()
    release = threading.Event()
    reads = 0

    def flaky_read(path, *, maximum_bytes):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            assert release.wait(5)
            raise OSError("temporary read failure")
        return read_record(path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(scene_repository, "_read_json_record", flaky_read)
    queue = SceneRuntimePersistenceQueue(repository, initial, retry_delay_seconds=0.001)
    latest = replace(initial, revision=3)
    try:
        queue.save(replace(initial, revision=1), expected_revision=0)
        assert started.wait(1)
        queue.save(replace(initial, revision=2), expected_revision=1)
        queue.save(latest, expected_revision=2)
        release.set()
        assert queue.flush()
        assert reads == 2
        assert repository.load_or_create(document) == latest
        assert queue.last_error is None
        assert queue.close()
    finally:
        release.set()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


def test_runtime_persistence_queue_close_seals_admission_and_drains_accepted_writes() -> None:
    initial = create_default_runtime_state(_document())
    repository = _GatedRuntimeRepository()
    queue = SceneRuntimePersistenceQueue(repository, initial)
    latest = replace(initial, revision=2)
    results: list[bool] = []
    closer = threading.Thread(target=lambda: results.append(queue.close(1)))
    try:
        queue.save(replace(initial, revision=1), expected_revision=0)
        assert repository.started.wait(1)
        queue.save(latest, expected_revision=1)
        closer.start()
        with queue._condition:
            assert queue._condition.wait_for(lambda: queue._closing, timeout=1)
        with pytest.raises(SceneRepositoryError, match="closed"):
            queue.save(latest, expected_revision=2)
        repository.release.set()
        closer.join(1)
        assert not closer.is_alive()
        assert results == [True]
        assert [state.revision for state in repository.saved] == [1, 2]
        assert not queue._thread.is_alive()
        assert queue.flush(0)
        assert queue.close(0)
    finally:
        repository.release.set()
        if closer.ident is not None:
            closer.join(2)
        queue.close()
        queue._thread.join(1)
        assert not closer.is_alive()
        assert not queue._thread.is_alive()


@pytest.mark.parametrize("failure", [None, OSError("temporary failure")])
def test_runtime_persistence_queue_timeout_preserves_inflight_and_stops_later_writes(
    failure,
) -> None:
    initial = create_default_runtime_state(_document())
    repository = _GatedRuntimeRepository(failure)
    queue = SceneRuntimePersistenceQueue(repository, initial)
    first = replace(initial, revision=1)
    latest = replace(initial, revision=2)
    try:
        queue.save(first, expected_revision=0)
        assert repository.started.wait(1)
        started = time.monotonic()
        assert not queue.flush(0.01)
        assert time.monotonic() - started < 0.3
        queue.save(latest, expected_revision=1)
        started = time.monotonic()
        assert not queue.close(0.01)
        assert time.monotonic() - started < 0.3
        assert queue._thread.is_alive()
        assert queue._inflight
        assert queue._pending is latest
        with pytest.raises(SceneRepositoryError, match="closed"):
            queue.save(replace(initial, revision=3), expected_revision=2)

        repository.release.set()
        assert not queue.close(1)
        assert not queue._thread.is_alive()
        assert not queue._inflight
        assert queue._accepted_state is latest
        assert queue._pending is latest
        assert repository.attempts == 1
        assert repository.saved == ([first] if failure is None else [])
        assert queue.last_error is failure
        started = time.monotonic()
        assert not queue.flush(1)
        assert time.monotonic() - started < 0.3
    finally:
        repository.release.set()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


def test_runtime_persistence_queue_close_interrupts_long_retry_backoff() -> None:
    initial = create_default_runtime_state(_document())
    failure = OSError("storage unavailable")
    repository = _GatedRuntimeRepository(failure)
    queue = SceneRuntimePersistenceQueue(repository, initial, retry_delay_seconds=60)
    latest = replace(initial, revision=2)
    try:
        queue.save(replace(initial, revision=1), expected_revision=0)
        repository.release.set()
        with queue._condition:
            assert queue._condition.wait_for(lambda: queue.last_error is failure, timeout=1)
            # A new save must coalesce without bypassing the retry delay.
            queue.save(latest, expected_revision=1)
        started = time.monotonic()
        assert not queue.close(0.02)
        assert time.monotonic() - started < 0.3
        queue._thread.join(1)
        assert not queue._thread.is_alive()
        assert repository.attempts == 1
        assert queue.last_error is failure
        assert queue._pending is latest
        started = time.monotonic()
        assert not queue.flush(1)
        assert time.monotonic() - started < 0.3
    finally:
        repository.release.set()
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


@pytest.mark.parametrize("delay", [0, -1, float("nan"), float("inf")])
def test_runtime_persistence_queue_rejects_invalid_retry_delays(delay) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        SceneRuntimePersistenceQueue(
            None, create_default_runtime_state(_document()), retry_delay_seconds=delay
        )


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_runtime_persistence_queue_rejects_invalid_timeouts_without_closing(timeout) -> None:
    initial = create_default_runtime_state(_document())
    queue = SceneRuntimePersistenceQueue(None, initial)
    try:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            queue.flush(timeout)
        with pytest.raises(ValueError, match="finite and nonnegative"):
            queue.close(timeout)
        queue.save(initial, expected_revision=0)
        assert queue.flush(0)
        assert queue.close()
    finally:
        queue.close()
        queue._thread.join(1)
        assert not queue._thread.is_alive()


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

    assert restored.schema_version == 3
    assert all(output.mode is OutputMode.AUTO for output in restored.outputs)
    assert {
        output.manual_scene_id for output in restored.outputs
    } == {CAMERA_SCENE_ID}


def test_selecting_a_scene_restores_automation_on_a_pinned_output() -> None:
    """A card click means "rest on this scene"; media must still take the output.

    Leaving the mode alone made select_scene a no-op on an already-pinned output:
    resolve_scene short-circuits on MANUAL, so the automation map was never read
    and playing media never reached the screen.
    """
    documents, runtime = _services()
    runtime.take_program_scene(CONTENT_CAMERA_PIP_SCENE_ID)  # pins every output
    assert all(output.mode is OutputMode.MANUAL for output in runtime.state.outputs)

    runtime.select_scene(BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID)

    program = runtime.state.output(BusId.VIRTUAL_CAMERA)
    assert program.mode is OutputMode.AUTO
    assert program.manual_scene_id == CAMERA_SCENE_ID
    # Idle rests on the chosen scene…
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.IDLE) == CAMERA_SCENE_ID
    # …and playing media still takes it over.
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.VIDEO) == CONTENT_SCENE_ID
    # The other output keeps its own pin: selecting is per-output.
    assert runtime.state.output(BusId.MEDIA_WINDOWS).mode is OutputMode.MANUAL


def test_taking_a_scene_still_pins_the_output_against_automation() -> None:
    documents, runtime = _services()

    runtime.take_scene(BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID)

    assert runtime.state.output(BusId.VIRTUAL_CAMERA).mode is OutputMode.MANUAL
    assert runtime.resolve_scene(BusId.VIRTUAL_CAMERA, ContentCategory.VIDEO) == CAMERA_SCENE_ID


def test_turning_off_auto_switch_keeps_each_output_on_its_own_scene() -> None:
    """Disabling automation pins the outputs — each to what it was showing.

    Pinning them all to Program's scene threw away the projection output's
    independent selection, which is the whole point of having two outputs.
    """
    documents, runtime = _services()
    runtime.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    runtime.select_scene(BusId.VIRTUAL_CAMERA, CONTENT_CAMERA_PIP_SCENE_ID)

    runtime.set_program_automatic(False, scene_ids={
        output.bus_id: runtime.resolve_scene(output.bus_id, ContentCategory.IDLE)
        for output in runtime.state.outputs
    })

    assert all(output.mode is OutputMode.MANUAL for output in runtime.state.outputs)
    assert runtime.state.output(BusId.MEDIA_WINDOWS).manual_scene_id == CAMERA_SCENE_ID
    assert (
        runtime.state.output(BusId.VIRTUAL_CAMERA).manual_scene_id
        == CONTENT_CAMERA_PIP_SCENE_ID
    )
