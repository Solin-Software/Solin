from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from threading import Event
from types import SimpleNamespace

import pytest

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.collections import DEFAULT_SCENE_COLLECTION_ID
from solin.core.scenes.model import (
    BusId,
    CameraPreset,
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    OnvifPtzBinding,
    NO_SIGNAL_SOURCE_ID,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
    DEFAULT_SCENE_ID,
    YEARTEXT_SOURCE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import SceneDocumentRepository
from solin.core.scenes.repository import SceneCollectionDocumentRepository
from solin.core.scenes.resources import SceneResourceCatalog
from solin.core.scenes.workspace import (
    SceneWorkspaceBusyError,
    SceneWorkspaceChangeKind,
    SceneWorkspaceOperationError,
)


def _paths(tmp_path: Path, profile_id: str = "profile-a") -> ProfilePaths:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id=profile_id,
    )
    paths.ensure_dirs()
    return paths


def _names() -> SceneSeedNames:
    return SceneSeedNames(
        content_source="Current content",
        default_camera_source="Default camera",
        no_signal_source="No signal background",
        content_scene="Content",
        camera_scene="Camera",
        content_camera_pip_scene="Content and camera",
        no_signal_scene="No signal",
        content_layer="Content",
        camera_layer="Camera",
        background_layer="Background",
    )


class _Projection:
    state = {"type": "idle"}

    def subscribe(self, _listener):
        return lambda: None


@contextmanager
def _workspace_cleanup_scope(tmp_path: Path):
    from tests.conftest import scene_workspace_factory

    with ExitStack() as finalizers:
        request = SimpleNamespace(addfinalizer=finalizers.callback)
        create = scene_workspace_factory.__wrapped__(request, tmp_path)
        yield create


def test_workspace_factory_cleans_owned_writers_after_an_assertion(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="simulated assertion failure"):
        with _workspace_cleanup_scope(tmp_path) as create:
            first = create(_paths(tmp_path, "first"), seed_names=_names())
            second = create(_paths(tmp_path, "second"), seed_names=_names())
            first.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
            second.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
            raise AssertionError("simulated assertion failure")

    for workspace in (first, second):
        assert workspace._runtime_persistence.flush(timeout_seconds=0)
        assert not workspace._runtime_persistence._thread.is_alive()


def test_workspace_factory_finalizes_failed_setup_before_temporary_storage(tmp_path: Path) -> None:
    report = tmp_path / "teardown.json"
    test_file = tmp_path / "test_failed_setup.py"
    test_file.write_text(
        dedent(f"""
            import json
            from pathlib import Path
            import pytest
            from tests.conftest import scene_workspace_factory
            from solin.core.scenes.model import BusId
            from tests.unit.test_scene_workspace import _names, _paths

            queues = []

            @pytest.fixture
            def tmp_path(tmp_path_factory):
                path = tmp_path_factory.mktemp("owned-storage")
                yield path
                state = {{
                    "storage_exists": path.exists(),
                    "flushed": all(queue.flush(0) for queue in queues),
                    "writers_stopped": all(not queue._thread.is_alive() for queue in queues),
                }}
                Path({str(report)!r}).write_text(json.dumps(state), encoding="utf-8")
                assert len(queues) == 1
                assert all(state.values())

            @pytest.fixture
            def failed_setup(scene_workspace_factory, tmp_path):
                workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
                queues.append(workspace._runtime_persistence)
                workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
                raise RuntimeError("simulated fixture setup failure")

            def test_failed_setup(failed_setup):
                pass
            """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-q",
            f"--confcutdir={tmp_path}",
            f"--basetemp={tmp_path / 'child-tmp'}",
            "-o",
            "tmp_path_retention_policy=all",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "simulated fixture setup failure" in result.stdout
    assert "1 error" in result.stdout, result.stdout + result.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "storage_exists": True,
        "flushed": True,
        "writers_stopped": True,
    }


def test_workspace_factory_drains_every_writer_when_one_close_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="simulated runtime close failure"):
        with _workspace_cleanup_scope(tmp_path) as create:
            first = create(_paths(tmp_path, "first"), seed_names=_names())
            second = create(_paths(tmp_path, "second"), seed_names=_names())
            original_close = second.runtime.close

            def fail_close() -> None:
                original_close()
                raise RuntimeError("simulated runtime close failure")

            monkeypatch.setattr(second.runtime, "close", fail_close)
            first.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
            second.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)

    for workspace in (first, second):
        assert workspace._runtime_persistence.flush(timeout_seconds=0)
        assert not workspace._runtime_persistence._thread.is_alive()


def test_workspace_factory_does_not_close_a_controllers_workspace_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls = []
    with _workspace_cleanup_scope(tmp_path) as create:
        workspace = create(_paths(tmp_path), seed_names=_names())
        original_close = workspace.close

        def count_close() -> None:
            close_calls.append(workspace)
            original_close()

        monkeypatch.setattr(workspace, "close", count_close)
        controller = SceneRuntimeController(workspace, _Projection())
        controller.close()

    assert close_calls == [workspace]
    assert not workspace._runtime_persistence._thread.is_alive()


def test_workspace_factory_joins_a_writer_after_early_controller_close_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    queue = None
    join_calls = []
    try:
        with _workspace_cleanup_scope(tmp_path) as create:
            workspace = create(_paths(tmp_path), seed_names=_names())
            queue = workspace._runtime_persistence
            original_save = queue._repository.save
            original_close = queue.close
            original_join = queue._thread.join

            def save(state, *, expected_revision=None) -> None:
                entered.set()
                assert release.wait(5), "test did not release its owned writer"
                original_save(state, expected_revision=expected_revision)

            monkeypatch.setattr(queue._repository, "save", save)
            monkeypatch.setattr(queue, "close", lambda: original_close(timeout_seconds=0))
            controller = SceneRuntimeController(workspace, _Projection())
            workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
            assert entered.wait(2)
            controller.close()
            assert queue._closing and queue._thread.is_alive()

            def join(timeout=None) -> None:
                join_calls.append(timeout)
                release.set()
                original_join(timeout)

            monkeypatch.setattr(queue._thread, "join", join)

        assert join_calls == [2.0]
        assert queue.flush(timeout_seconds=0)
        assert not queue._thread.is_alive()
    finally:
        release.set()
        if queue is not None:
            queue._thread.join(2)
            assert not queue._thread.is_alive()


@pytest.mark.parametrize("failure", ["write_error", "blocked_writer"])
def test_workspace_factory_reports_unsaved_state_and_a_still_live_owned_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    entered = Event()
    release = Event()
    queue = None
    try:
        with pytest.raises(AssertionError, match="flushed=False") as caught:
            with _workspace_cleanup_scope(tmp_path) as create:
                workspace = create(_paths(tmp_path), seed_names=_names())
                queue = workspace._runtime_persistence
                original_save = queue._repository.save
                original_close = queue.close

                def save(state, *, expected_revision=None) -> None:
                    entered.set()
                    if failure == "write_error":
                        raise OSError("simulated persistence failure")
                    if not release.wait(5):
                        raise TimeoutError("test did not release its owned writer")
                    original_save(state, expected_revision=expected_revision)

                monkeypatch.setattr(queue._repository, "save", save)
                monkeypatch.setattr(queue, "close", lambda: original_close(timeout_seconds=0.05))
                workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
                assert entered.wait(2), "owned writer did not enter the storage call"

        if failure == "blocked_writer":
            assert "writer_stopped=False" in str(caught.value)
    finally:
        release.set()
        if queue is not None:
            queue._thread.join(2)
            assert not queue._thread.is_alive()


def test_fresh_workspace_has_one_minimal_profile_and_internal_fallback(scene_workspace_factory, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())

    assert workspace.active_collection.id == DEFAULT_SCENE_COLLECTION_ID
    assert workspace.active_collection.name == "Profile 1"
    assert [scene.id for scene in workspace.documents.document.scenes] == [
        DEFAULT_SCENE_ID,
        CAMERA_SCENE_ID,
        CONTENT_SCENE_ID,
    ]
    assert {source.id for source in workspace.documents.document.sources} == {
        CONTENT_SOURCE_ID,
        DEFAULT_CAMERA_SOURCE_ID,
        NO_SIGNAL_SOURCE_ID,
        YEARTEXT_SOURCE_ID,
    }
    assert workspace.documents.program_default_scene_id == DEFAULT_SCENE_ID
    assert workspace.documents.program_media_scene_id == CONTENT_SCENE_ID
    assert (
        paths.scene_profiles_dir / f"{DEFAULT_SCENE_COLLECTION_ID}.backup.json"
    ).exists()


def test_fresh_workspace_default_scene_holds_the_year_text_source(scene_workspace_factory, tmp_path: Path) -> None:
    # First run: the Default scene (the idle fallback) shows the year text, so the
    # projection and virtual camera both fall back to it when nothing else plays.
    from solin.core.scenes.model import SourceKind

    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    document = workspace.documents.document

    yeartext_sources = [
        source for source in document.sources if source.kind is SourceKind.YEARTEXT
    ]
    assert [source.id for source in yeartext_sources] == [YEARTEXT_SOURCE_ID]

    default_scene = document.scene(DEFAULT_SCENE_ID)
    assert [layer.source_id for layer in default_scene.layers] == [YEARTEXT_SOURCE_ID]
    # The Default scene is the shared idle fallback for every output bus.
    assert {route.default_scene_id for route in document.outputs} == {DEFAULT_SCENE_ID}


def test_legacy_document_gains_the_year_text_default_scene_on_load(scene_workspace_factory, tmp_path: Path) -> None:
    # A collection saved before the year-text-as-a-scene feature has no Default
    # scene, so its idle projection would have no year text after upgrading. On
    # load it is self-healed: the year-text source + Default scene are added and
    # the Default scene becomes the shared idle default, while the legacy scenes,
    # sources, and document identity are preserved.
    paths = _paths(tmp_path)
    legacy = create_default_scene_document(
        _names(),
        document_id="legacy-document",
        created_at="2026-08-10T12:00:00+00:00",
    )
    assert all(scene.id != DEFAULT_SCENE_ID for scene in legacy.scenes)  # precondition
    assert all(source.id != YEARTEXT_SOURCE_ID for source in legacy.sources)
    SceneDocumentRepository(paths.scenes_file, seed_factory=lambda: legacy).save(legacy)

    first = scene_workspace_factory(paths, seed_names=_names())
    healed = first.documents.document
    first.close()

    yeartext_sources = [s for s in healed.sources if s.kind is SourceKind.YEARTEXT]
    assert [s.id for s in yeartext_sources] == [YEARTEXT_SOURCE_ID]
    default_scene = healed.scene(DEFAULT_SCENE_ID)
    assert [layer.source_id for layer in default_scene.layers] == [YEARTEXT_SOURCE_ID]
    assert {route.default_scene_id for route in healed.outputs} == {DEFAULT_SCENE_ID}
    # Legacy scenes + identity survive; nothing was dropped.
    assert healed.document_id == "legacy-document"
    assert {scene.id for scene in legacy.scenes} <= {scene.id for scene in healed.scenes}

    # Idempotent: reloading heals to the same structure — no duplicate Default
    # scene or year-text source.
    second = scene_workspace_factory(paths, seed_names=_names())
    reloaded = second.documents.document
    assert [s.id for s in reloaded.scenes] == [s.id for s in healed.scenes]
    assert sum(s.kind is SourceKind.YEARTEXT for s in reloaded.sources) == 1
    assert sum(scene.id == DEFAULT_SCENE_ID for scene in reloaded.scenes) == 1
    assert {route.default_scene_id for route in reloaded.outputs} == {DEFAULT_SCENE_ID}
    assert len(second.catalog.collections) == 1


def test_legacy_backup_is_migrated_and_normalized_to_shared_camera_refs(
    scene_workspace_factory,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    legacy = create_default_scene_document(
        _names(),
        document_id="legacy-document",
        created_at="2026-08-10T12:00:00+00:00",
    )
    legacy_repository = SceneDocumentRepository(
        paths.scenes_file,
        seed_factory=lambda: legacy,
    )
    legacy_repository.save(legacy)
    legacy_repository.initialize_backup(replace(legacy, revision=1))

    scene_workspace_factory(paths, seed_names=_names())

    raw_backup = json.loads(
        (
            paths.scene_profiles_dir
            / f"{DEFAULT_SCENE_COLLECTION_ID}.backup.json"
        ).read_text(encoding="utf-8")
    )
    camera = next(
        source for source in raw_backup["sources"]
        if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    assert raw_backup["document_id"] == "legacy-document"
    assert camera == {
        "id": DEFAULT_CAMERA_SOURCE_ID,
        "type": "camera_ref",
        "enabled": True,
    }


def test_corrupt_active_profile_recovers_from_its_backup(scene_workspace_factory, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    expected_document = workspace.documents.document
    workspace.close()
    primary = paths.scene_profiles_dir / f"{DEFAULT_SCENE_COLLECTION_ID}.json"
    primary.write_text("{not-json", encoding="utf-8")

    recovered = scene_workspace_factory(paths, seed_names=_names())

    assert recovered.documents.document == expected_document
    assert json.loads(primary.read_text(encoding="utf-8"))["document_id"] == (
        expected_document.document_id
    )


def test_profile_mutations_are_persisted_before_publication(scene_workspace_factory, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        paths,
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    observed: list[tuple[SceneWorkspaceChangeKind, str]] = []

    def observe(change) -> None:
        persisted = paths.scene_profiles_catalog_file.read_text(encoding="utf-8")
        assert change.catalog.active_collection_id in persisted
        observed.append((change.kind, change.collection.id))

    workspace.subscribe(observe)
    created = workspace.create_collection("Auditorium")
    workspace.activate_collection(created.id)
    workspace.rename_collection(created.id, "Main auditorium")

    assert observed == [
        (SceneWorkspaceChangeKind.CATALOG, DEFAULT_SCENE_COLLECTION_ID),
        (SceneWorkspaceChangeKind.ACTIVATED, created.id),
        (SceneWorkspaceChangeKind.CATALOG, created.id),
    ]
    assert workspace.documents.can_undo is False
    assert workspace.documents.can_redo is False


def test_failed_profile_creation_removes_unpublished_document(
    scene_workspace_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(
        paths,
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )

    def fail_catalog_save(*_args, **_kwargs) -> None:
        raise OSError("simulated catalog write failure")

    monkeypatch.setattr(workspace._catalog_repository, "save", fail_catalog_save)

    with pytest.raises(OSError, match="simulated catalog write failure"):
        workspace.create_collection("Auditorium")

    assert not (paths.scene_profiles_dir / "collection-b.json").exists()
    assert not (paths.scene_profiles_dir / "collection-b.backup.json").exists()
    assert [collection.id for collection in workspace.catalog.collections] == [
        DEFAULT_SCENE_COLLECTION_ID
    ]


def test_scene_profiles_preserve_output_preferences_when_switching(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    created = workspace.create_collection("Auditorium")
    workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)

    workspace.activate_collection(created.id)

    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).enabled is True
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).enabled is False


def test_scene_profiles_keep_independent_program_transition_policies(
    scene_workspace_factory,
    tmp_path: Path,
) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    first_collection_id = workspace.active_collection.id
    first_spec = TransitionSpec(TransitionKind.FADE_TO_BLACK, 800)
    workspace.documents.set_program_transition(first_spec)
    created = workspace.create_collection("Auditorium")

    workspace.activate_collection(created.id)

    assert workspace.documents.document.transition_policy.default == TransitionSpec(
        TransitionKind.DISSOLVE,
        350,
    )
    workspace.documents.set_program_transition(TransitionSpec(TransitionKind.CUT, 0))
    workspace.activate_collection(first_collection_id)
    assert workspace.documents.document.transition_policy.default == first_spec


def test_activation_commit_failure_rolls_back_catalog_and_runtime(
    scene_workspace_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    created = workspace.create_collection("Auditorium")
    workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
    original_state = workspace.runtime.state
    original_save = workspace._catalog_repository.save
    save_calls = 0

    def fail_final_catalog_save(catalog, *, expected_revision=None) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("simulated final catalog write failure")
        original_save(catalog, expected_revision=expected_revision)

    monkeypatch.setattr(
        workspace._catalog_repository,
        "save",
        fail_final_catalog_save,
    )

    with pytest.raises(OSError, match="simulated final catalog write failure"):
        workspace.activate_collection(created.id)

    persisted_catalog = workspace._catalog_repository.load()
    assert workspace.active_collection.id == DEFAULT_SCENE_COLLECTION_ID
    assert persisted_catalog.active_collection_id == DEFAULT_SCENE_COLLECTION_ID
    assert persisted_catalog.pending_collection_id == ""
    assert workspace.runtime.state == original_state
    workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, False)


def test_virtual_camera_blocks_create_switch_and_delete_but_not_rename(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(
        ("default-document", "collection-b", "document-b", "collection-c", "document-c")
    )
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    created = workspace.create_collection("Auditorium")
    workspace.runtime.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    with pytest.raises(SceneWorkspaceBusyError):
        workspace.create_collection("Special meeting")
    with pytest.raises(SceneWorkspaceBusyError):
        workspace.activate_collection(created.id)
    with pytest.raises(SceneWorkspaceBusyError):
        workspace.delete_collection(created.id)

    renamed = workspace.rename_collection(created.id, "Main auditorium")
    assert renamed.name == "Main auditorium"


def test_delete_active_requires_and_activates_replacement_first(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(
        paths,
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    created = workspace.create_collection("Auditorium")
    workspace.activate_collection(created.id)

    with pytest.raises(SceneWorkspaceOperationError):
        workspace.delete_collection(created.id)

    workspace.delete_collection(
        created.id,
        replacement_collection_id=DEFAULT_SCENE_COLLECTION_ID,
    )

    assert workspace.active_collection.id == DEFAULT_SCENE_COLLECTION_ID
    assert not (paths.scene_profiles_dir / f"{created.id}.json").exists()
    with pytest.raises(SceneWorkspaceOperationError):
        workspace.delete_collection(DEFAULT_SCENE_COLLECTION_ID)


def test_scene_profile_names_are_unique_ignoring_case_and_whitespace(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    workspace.create_collection("Main Auditorium")

    with pytest.raises(ValueError):
        workspace.create_collection("  main   auditorium  ")


def test_runtime_controller_rebinds_to_the_activated_scene_profile(
    request, scene_workspace_factory, tmp_path: Path
) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    workspace.documents.create_scene("Temporary edit")
    created = workspace.create_collection("Auditorium")
    controller = SceneRuntimeController(workspace, _Projection())
    request.addfinalizer(controller.close)

    workspace.activate_collection(created.id)

    assert controller.documents is workspace.documents
    assert controller.runtime is workspace.runtime
    assert controller.document.document_id == "document-b"
    assert controller.documents.can_undo is False
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == DEFAULT_SCENE_ID


def test_camera_configuration_is_shared_between_scene_profiles(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(
        paths,
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    camera = workspace.documents.document.source(DEFAULT_CAMERA_SOURCE_ID)
    configured = replace(
        camera,
        name="Pulpit camera",
        configuration=LocalCameraConfig(device_id="physical-camera-1"),
    )
    workspace.upsert_camera(configured)
    created = workspace.create_collection("Auditorium")

    workspace.activate_collection(created.id)

    assert workspace.documents.document.source(DEFAULT_CAMERA_SOURCE_ID) == configured
    assert workspace.configured_cameras == (configured,)
    assert paths.scene_resources_file.exists()
    stored = json.loads(
        (paths.scene_profiles_dir / f"{created.id}.json").read_text(encoding="utf-8")
    )
    camera_record = next(source for source in stored["sources"] if source["id"] == camera.id)
    assert camera_record == {
        "id": DEFAULT_CAMERA_SOURCE_ID,
        "type": "camera_ref",
        "enabled": True,
    }


def test_shared_cameras_are_isolated_between_general_solin_profiles(scene_workspace_factory, tmp_path: Path) -> None:
    first = scene_workspace_factory(_paths(tmp_path, "first"), seed_names=_names())
    second = scene_workspace_factory(_paths(tmp_path, "second"), seed_names=_names())
    first_camera = first.documents.document.source(DEFAULT_CAMERA_SOURCE_ID)

    first.upsert_camera(
        replace(
            first_camera,
            name="First profile camera",
            configuration=LocalCameraConfig(device_id="physical-camera-1"),
        )
    )

    assert first.configured_cameras[0].name == "First profile camera"
    assert second.configured_cameras[0].name == "Default camera"


def test_orphaned_camera_cleanup_waits_for_backup_and_retries_credentials(
    scene_workspace_factory,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = workspace.documents.document.source(DEFAULT_CAMERA_SOURCE_ID)
    workspace.upsert_camera(
        replace(
            camera,
            configuration=LocalCameraConfig(
                device_id="physical-camera-1",
                ptz_binding=OnvifPtzBinding(
                    endpoint="http://192.0.2.10/onvif/device_service",
                    credential_ref="ptz-0123456789abcdef0123456789abcdef",
                ),
            ),
        )
    )
    camera_layer = next(
        layer
        for layer in workspace.documents.document.scene(CAMERA_SCENE_ID).layers
        if layer.source_id == DEFAULT_CAMERA_SOURCE_ID
    )

    workspace.documents.delete_layer_and_orphaned_source(
        CAMERA_SCENE_ID,
        camera_layer.id,
    )

    assert workspace.configured_cameras
    workspace.close()

    restarted = scene_workspace_factory(paths, seed_names=_names())

    def unavailable_vault(_reference: str) -> None:
        raise OSError("vault unavailable")

    restarted.set_credential_cleaner(unavailable_vault)
    restarted.documents.rename_scene(CONTENT_SCENE_ID, "Projected content")

    assert restarted.configured_cameras == ()
    assert restarted.resources.pending_credential_deletions == (
        "ptz-0123456789abcdef0123456789abcdef",
    )
    restarted.close()

    deleted_credentials: list[str] = []
    recovered = scene_workspace_factory(paths, seed_names=_names())
    recovered.set_credential_cleaner(deleted_credentials.append)

    assert deleted_credentials == ["ptz-0123456789abcdef0123456789abcdef"]
    assert recovered.resources.pending_credential_deletions == ()


def test_ptz_presets_are_shared_while_scene_actions_remain_collection_local(
    scene_workspace_factory,
    tmp_path: Path,
) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    camera = workspace.documents.document.source(DEFAULT_CAMERA_SOURCE_ID)
    workspace.upsert_camera(
        replace(
            camera,
            configuration=LocalCameraConfig(
                ptz_binding=OnvifPtzBinding(
                    endpoint="http://192.0.2.10/onvif/device_service"
                )
            ),
        )
    )
    preset = CameraPreset(
        id="preset-pulpit",
        camera_source_id=DEFAULT_CAMERA_SOURCE_ID,
        name="Pulpit",
        remote_token="1",
    )
    workspace.create_camera_preset(preset)
    created = workspace.create_collection("Auditorium")

    workspace.activate_collection(created.id)

    assert workspace.documents.document.camera_presets == (preset,)
    assert all(not scene.entry_actions for scene in workspace.documents.document.scenes)


def test_deleting_scene_profile_collects_its_unreferenced_camera(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    created = workspace.create_collection("Auditorium")
    workspace.activate_collection(created.id)
    camera = SourceDefinition(
        id="auditorium-camera",
        kind=SourceKind.LOCAL_CAMERA,
        name="Auditorium camera",
        configuration=LocalCameraConfig(device_id="camera://auditorium"),
    )
    workspace.upsert_camera(camera, add_to_active_document=True)
    workspace.activate_collection(DEFAULT_SCENE_COLLECTION_ID)

    workspace.delete_collection(created.id)

    assert all(
        configured.id != camera.id for configured in workspace.configured_cameras
    )


def test_new_scene_profile_uses_the_configured_camera_identity(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    workspace = scene_workspace_factory(
        _paths(tmp_path),
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    camera = SourceDefinition(
        id="configured-camera",
        kind=SourceKind.LOCAL_CAMERA,
        name="Configured camera",
        configuration=LocalCameraConfig(device_id="camera://configured"),
    )
    resources = replace(
        workspace.resources,
        revision=workspace.resources.revision + 1,
        cameras=(camera,),
    )
    workspace._resource_repository.save(
        resources,
        expected_revision=workspace.resources.revision,
    )
    workspace._resources = resources

    created = workspace.create_collection("Auditorium")
    activated = workspace.activate_collection(created.id)

    assert activated.id == created.id
    assert workspace.documents.document.source(camera.id) == camera
    assert all(
        layer.source_id != DEFAULT_CAMERA_SOURCE_ID
        for scene in workspace.documents.document.scenes
        for layer in scene.layers
    )


def test_legacy_fresh_profile_camera_reference_recovers_to_configured_camera(
    tmp_path: Path,
) -> None:
    camera = SourceDefinition(
        id="configured-camera",
        kind=SourceKind.LOCAL_CAMERA,
        name="Configured camera",
        configuration=LocalCameraConfig(device_id="camera://configured"),
    )
    resources = SceneResourceCatalog(revision=0, cameras=(camera,))
    path = tmp_path / "profile.json"
    repository = SceneCollectionDocumentRepository(
        path,
        resources=lambda: resources,
    )
    document = create_default_scene_document(
        _names(),
        document_id="legacy-fresh-profile",
        created_at="2026-08-11T00:00:00+00:00",
    )
    repository.save(document)
    repository.initialize_backup(document)

    recovered = repository.load()
    repository.normalize_storage()
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert recovered.source(camera.id) == camera
    assert all(
        layer.source_id != DEFAULT_CAMERA_SOURCE_ID
        for scene in recovered.scenes
        for layer in scene.layers
    )
    assert any(
        source.get("id") == camera.id and source.get("type") == "camera_ref"
        for source in persisted["sources"]
    )
