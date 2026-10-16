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
from solin.core.media import camera_identity
from solin.core.scenes.collections import DEFAULT_SCENE_COLLECTION_ID
from solin.core.scenes.application import SceneNotFoundError
from solin.core.scenes.model import (
    BusId,
    CameraMediaType,
    CameraPreset,
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    FitMode,
    LocalCameraConfig,
    OnvifPtzBinding,
    RtspCameraConfig,
    SceneLayer,
    SceneValidationError,
    NO_SIGNAL_SOURCE_ID,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
    SolinContentMode,
    stable_identity,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import SceneDocumentRepository
from solin.core.scenes.repository import SceneCollectionDocumentRepository
from solin.core.scenes.resources import SceneResourceCatalog
from solin.core.scenes.workspace import (
    LocalCameraConflictError,
    SceneCameraApplyError,
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
    idle_media_path = ""

    def set_idle_media_path(self, path):
        self.idle_media_path = path

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
    assert [scene.id for scene in workspace.documents.document.scenes] == [CONTENT_SCENE_ID, CAMERA_SCENE_ID]
    assert {source.id for source in workspace.documents.document.sources} == {CONTENT_SOURCE_ID, NO_SIGNAL_SOURCE_ID}
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID
    assert workspace.documents.program_media_scene_id == CONTENT_SCENE_ID


def _beta_preset_record(*, customized=False):
    from copy import deepcopy

    seed = create_default_scene_document(_names(), document_id="beta-document", created_at="2026-08-10T12:00:00+00:00")
    record = replace(seed, scenes=seed.scenes[:2]).to_record()
    record["schema_version"] = 11
    record["scenes"][1]["layers"][0]["fit_mode"] = "cover"
    content = record["sources"][0]
    content["configuration"] = {}
    content["name"] = "Current Solin content"
    idle = deepcopy(content)
    idle.update(id="solin.idle.current", type="idle_screen", name="Idle screen")
    record["sources"].append(idle)
    default = deepcopy(record["scenes"][0])
    default.update(id=stable_identity("default-scene:default"), name="Default")
    default["layers"][0].update(id=stable_identity("default-layer:default:idle"), source_id=idle["id"], name="Idle screen")
    record["scenes"] = [default, record["scenes"][1], record["scenes"][0]]
    for route in record["outputs"]:
        route["default_scene_id"] = default["id"]
    if customized:
        default["name"] = "My idle"
        default["layers"][0]["rect"]["width"] = 0.7
        default["layers"].append(dict(default["layers"][0], id="duplicate-idle"))
        record["outputs"][0]["default_scene_id"] = CAMERA_SCENE_ID
    return record


@pytest.mark.parametrize("customized", [False, True])
def test_beta_migration_preserves_custom_collections_and_rebuilds_only_pristine(scene_workspace_factory, tmp_path, customized):
    paths = _paths(tmp_path)
    record = _beta_preset_record(customized=customized)
    paths.scenes_file.write_text(json.dumps(record), encoding="utf-8")
    workspace = scene_workspace_factory(paths, seed_names=_names())
    document = workspace.documents.document
    assert document.document_id == record["document_id"]
    if customized:
        assert [scene.id for scene in document.scenes] == [scene["id"] for scene in record["scenes"]]
        assert document.scenes[0].name == "My idle"
        assert document.scenes[0].to_record()["layers"] == record["scenes"][0]["layers"]
        assert document.scene(CAMERA_SCENE_ID).layers[0].fit_mode is FitMode.COVER
        assert document.source("solin.idle.current").configuration.mode is SolinContentMode.PRESENTATION
        assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CAMERA_SCENE_ID
        assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == record["scenes"][0]["id"]
    else:
        assert [scene.id for scene in document.scenes] == [CONTENT_SCENE_ID, CAMERA_SCENE_ID]
        assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID
        assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID
    workspace.close()
    reloaded = scene_workspace_factory(paths, seed_names=_names())
    assert reloaded.documents.document == document
    assert json.loads((paths.scene_profiles_dir / f"{DEFAULT_SCENE_COLLECTION_ID}.json").read_text())["schema_version"] == 13


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
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_camera_configuration_is_shared_between_scene_profiles(scene_workspace_factory, tmp_path: Path) -> None:
    identities = iter(("default-document", "collection-b", "document-b"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(
        paths,
        seed_names=_names(),
        identity_factory=lambda: next(identities),
    )
    camera = create_default_scene_document(_names(), document_id="camera-fixture").source(DEFAULT_CAMERA_SOURCE_ID)
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
    first_camera = create_default_scene_document(_names(), document_id="camera-fixture").source(DEFAULT_CAMERA_SOURCE_ID)

    first.upsert_camera(
        replace(
            first_camera,
            name="First profile camera",
            configuration=LocalCameraConfig(device_id="physical-camera-1"),
        )
    )

    assert first.configured_cameras[0].name == "First profile camera"
    assert second.configured_cameras == ()


def _local_camera(device_id: str = "physical-camera-1", *, camera_id: str = "shared-camera") -> SourceDefinition:
    return SourceDefinition(
        id=camera_id, kind=SourceKind.LOCAL_CAMERA, name="Shared camera",
        configuration=LocalCameraConfig(
            device_id=device_id, width=640, height=480,
            fps_numerator=30, fps_denominator=1,
            media_type=CameraMediaType.RAW, pixel_format="YUY2",
        ),
    )


def test_camera_occurrences_reuse_connection_without_changing_configuration(
    scene_workspace_factory, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(camera_identity, "sys", SimpleNamespace(platform="win32"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = _local_camera(r"Original:\\?\USB#CAMERA")
    first = SceneLayer(id="first-occurrence", source_id=camera.id, name="Lectern")
    before = workspace.documents.document
    inserted = workspace.add_camera_layer(CAMERA_SCENE_ID, camera, first)
    assert inserted == first
    assert workspace.documents.document.revision == before.revision + 1
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers[-1] == first
    stored_resources = paths.scene_resources_file.read_bytes()

    alias = replace(
        camera, id="proposed-duplicate", name="Another name",
        configuration=LocalCameraConfig(device_id=r"Renamed:\\?\usb#camera"),
    )
    second = SceneLayer(id="second-occurrence", source_id=alias.id, name="Wide angle")
    reused = workspace.add_camera_layer(CAMERA_SCENE_ID, alias, second)
    third = workspace.add_camera_layer(
        CONTENT_SCENE_ID, alias,
        SceneLayer(id="third-occurrence", source_id=alias.id, name="Picture in picture"),
    )

    assert reused.source_id == third.source_id == camera.id
    assert reused.name == "Wide angle"
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (first, reused)
    assert workspace.documents.document.source(camera.id) == camera
    assert workspace.configured_cameras == (camera,)
    assert paths.scene_resources_file.read_bytes() == stored_resources


@pytest.mark.parametrize("failing_store", ["catalog", "document"])
def test_camera_insertion_failure_restores_resources_document_and_history(
    scene_workspace_factory, tmp_path, monkeypatch, failing_store,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    before_document = workspace.documents.document
    before_resources = workspace.resources
    document_path = paths.scene_profiles_dir / f"{workspace.active_collection.id}.json"
    stored_document = document_path.read_bytes()
    changes = []
    unsubscribe = workspace.subscribe(changes.append)
    camera = _local_camera()

    def reject_save(*args, **kwargs):
        raise OSError("injected storage failure")

    target = workspace._resource_repository if failing_store == "catalog" else workspace.documents._store
    try:
        with monkeypatch.context() as patch:
            patch.setattr(target, "save", reject_save)
            with pytest.raises(OSError, match="injected storage failure"):
                workspace.add_camera_layer(
                    CAMERA_SCENE_ID, camera,
                    SceneLayer(id="failed-layer", source_id=camera.id, name="Camera"),
                )
    finally:
        unsubscribe()

    assert workspace.documents.document == before_document
    assert document_path.read_bytes() == stored_document
    assert workspace.resources.cameras == before_resources.cameras
    assert workspace._resource_repository.load() == workspace.resources
    assert changes == []
    assert not workspace.documents.can_undo
    assert not workspace.documents.can_redo
    workspace.close()
    reloaded = scene_workspace_factory(paths, seed_names=_names())
    assert reloaded.configured_cameras == ()
    assert reloaded.documents.document == before_document


def test_invalid_camera_occurrence_is_rejected_before_catalog_write(
    scene_workspace_factory, tmp_path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    before = workspace.documents.document
    resources = paths.scene_resources_file.read_bytes()
    camera = _local_camera()

    with pytest.raises(SceneNotFoundError, match="does not exist"):
        workspace.add_camera_layer(
            "missing-scene", camera,
            SceneLayer(id="missing-scene-layer", source_id=camera.id, name="Camera"),
        )

    assert workspace.documents.document == before
    assert paths.scene_resources_file.read_bytes() == resources
    assert not workspace.documents.can_undo


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_camera_reuse_preserves_case_sensitive_backend_identity(
    scene_workspace_factory, tmp_path, monkeypatch, platform,
) -> None:
    monkeypatch.setattr(camera_identity, "sys", SimpleNamespace(platform=platform))
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    first = _local_camera("Native-ID")
    second = _local_camera("native-id", camera_id="other-camera")
    workspace.upsert_camera(first)
    workspace.upsert_camera(second)

    assert workspace.local_camera_for_device("Native-ID") == first
    assert workspace.local_camera_for_device("native-id") == second
    assert workspace.local_camera_for_device("NATIVE-ID") is None


def test_unbound_camera_does_not_match_another_unbound_connection(scene_workspace_factory, tmp_path) -> None:
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    first = _local_camera("")
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, first,
        SceneLayer(id="unbound-first", source_id=first.id, name="First unbound"),
    )
    second = _local_camera("", camera_id="another-unbound")
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, second,
        SceneLayer(id="unbound-second", source_id=second.id, name="Second unbound"),
    )

    assert workspace.local_camera_for_device("") is None
    assert workspace.configured_cameras == (first, second)


@pytest.mark.parametrize("compatible", [False, True])
def test_legacy_camera_aliases_are_resolved_only_when_settings_are_equivalent(
    scene_workspace_factory, tmp_path, monkeypatch, compatible,
) -> None:
    monkeypatch.setattr(camera_identity, "sys", SimpleNamespace(platform="win32"))
    paths = _paths(tmp_path)
    first = _local_camera(r"Old name:\\?\USB#CAMERA", camera_id="camera-a")
    configuration = replace(first.configuration, device_id=r"New name:\\?\usb#camera")
    if not compatible:
        configuration = replace(configuration, fps_numerator=15)
    second = replace(first, id="camera-b", name="Other connection", configuration=configuration)
    legacy = SceneResourceCatalog(revision=4, cameras=(second, first))
    paths.scene_resources_file.write_text(json.dumps(legacy.to_record()), encoding="utf-8")
    workspace = scene_workspace_factory(paths, seed_names=_names())
    stored = paths.scene_resources_file.read_bytes()

    if compatible:
        assert workspace.local_camera_for_device(r"Current name:\\?\usb#camera") == first
    else:
        with pytest.raises(LocalCameraConflictError, match="Conflicting"):
            workspace.local_camera_for_device(r"Current name:\\?\usb#camera")
        with pytest.raises(LocalCameraConflictError, match="Conflicting"):
            workspace.add_camera_layer(
                CAMERA_SCENE_ID, first,
                SceneLayer(id="ambiguous-layer", source_id=first.id, name="Ambiguous"),
            )
    assert paths.scene_resources_file.read_bytes() == stored
    assert workspace.configured_cameras == (second, first)
    assert not workspace.documents.can_undo


def test_shared_camera_updates_survive_layer_undo_redo_and_reload(
    scene_workspace_factory, tmp_path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = _local_camera()
    first = SceneLayer(id="first-layer", source_id=camera.id, name="First camera")
    second = SceneLayer(id="second-layer", source_id=camera.id, name="Second camera")
    workspace.add_camera_layer(CAMERA_SCENE_ID, camera, first)
    workspace.add_camera_layer(CAMERA_SCENE_ID, camera, second)
    updated = replace(camera, configuration=replace(camera.configuration, fps_numerator=15))

    workspace.upsert_camera(updated)

    assert workspace.documents.document.source(camera.id) == updated
    workspace.documents.undo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (first,)
    assert workspace.documents.document.source(camera.id) == updated
    workspace.documents.undo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
    assert all(source.id != camera.id for source in workspace.documents.document.sources)
    assert workspace.configured_cameras == (updated,)
    workspace.documents.redo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (first,)
    assert workspace.documents.document.source(camera.id) == updated
    workspace.documents.redo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (first, second)
    assert workspace.documents.document.source(camera.id) == updated
    workspace.documents.rename_layer(CAMERA_SCENE_ID, first.id, "My camera")
    workspace.close()
    reloaded = scene_workspace_factory(paths, seed_names=_names())

    assert reloaded.documents.document.source(camera.id) == updated
    layers = reloaded.documents.document.scene(CAMERA_SCENE_ID).layers
    assert layers == (replace(first, name="My camera"), second)


@pytest.mark.parametrize("failing_store", ["catalog", "document"])
def test_failed_shared_camera_update_preserves_previous_settings_and_scene_history(
    scene_workspace_factory, tmp_path, monkeypatch, failing_store,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = _local_camera()
    layer = SceneLayer(id="camera-layer", source_id=camera.id, name="My camera")
    workspace.add_camera_layer(CAMERA_SCENE_ID, camera, layer)
    before = workspace.documents.document
    updated = replace(camera, configuration=replace(camera.configuration, fps_numerator=15))

    def reject_save(*args, **kwargs):
        raise OSError("injected update failure")

    target = workspace._resource_repository if failing_store == "catalog" else workspace.documents._store
    with monkeypatch.context() as patch:
        patch.setattr(target, "save", reject_save)
        with pytest.raises(OSError, match="injected update failure"):
            workspace.upsert_camera(updated)

    assert workspace.configured_cameras == (camera,)
    assert workspace._resource_repository.load() == workspace.resources
    assert workspace.documents.document == before
    workspace.documents.undo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
    assert not workspace.documents.can_undo
    workspace.documents.redo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (layer,)
    assert workspace.documents.document.source(camera.id) == camera


def test_shared_camera_double_persistence_failure_reports_durable_catalog_and_original_cause(
    scene_workspace_factory, tmp_path, monkeypatch,
) -> None:
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    camera = _local_camera()
    layer = SceneLayer(id="camera-layer", source_id=camera.id, name="My camera")
    workspace.add_camera_layer(CAMERA_SCENE_ID, camera, layer)
    before_document = workspace.documents.document
    document_path = workspace.documents._store.path
    stored_document = document_path.read_bytes()
    before_resources = workspace.resources
    updated = replace(camera, configuration=replace(camera.configuration, fps_numerator=15))
    document_error = OSError("document persistence failed")
    compensation_error = OSError("catalog compensation failed")
    save_resources = workspace._resource_repository.save
    catalog_attempts = []
    changes = []
    unsubscribe = workspace.subscribe(changes.append)

    def save_then_reject_compensation(resources, **kwargs):
        catalog_attempts.append(resources)
        if len(catalog_attempts) == 2:
            raise compensation_error
        return save_resources(resources, **kwargs)

    def reject_document(*args, **kwargs):
        raise document_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(workspace._resource_repository, "save", save_then_reject_compensation)
            patch.setattr(workspace.documents._store, "save", reject_document)
            with pytest.raises(SceneCameraApplyError) as failure:
                workspace.upsert_camera(updated)
    finally:
        unsubscribe()

    assert failure.value.__cause__ is document_error
    assert len(catalog_attempts) == 2
    assert catalog_attempts[1].cameras == (camera,)
    assert workspace.resources == replace(
        before_resources, revision=before_resources.revision + 1, cameras=(updated,),
    )
    assert workspace._resource_repository.load() == workspace.resources
    assert workspace.documents.document == before_document
    assert document_path.read_bytes() == stored_document
    assert [change.kind for change in changes] == [SceneWorkspaceChangeKind.RESOURCES]
    workspace.documents.undo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
    assert not workspace.documents.can_undo
    workspace.documents.redo()
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == (layer,)
    assert workspace.documents.document.source(camera.id) == updated


def test_ip_camera_reuse_preserves_connection_and_identical_urls_are_not_deduplicated(
    scene_workspace_factory, tmp_path,
) -> None:
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    first = SourceDefinition(
        id="first-ip", kind=SourceKind.RTSP_CAMERA, name="Front camera",
        configuration=RtspCameraConfig(uri="rtsp://camera.invalid/live"),
    )
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, first,
        SceneLayer(id="first-ip-layer", source_id=first.id, name="Front"),
    )
    reuse_proposal = replace(
        first, name="Changed connection", configuration=RtspCameraConfig(uri="rtsp://other.invalid/live"),
    )
    reused = workspace.add_camera_layer(
        CONTENT_SCENE_ID, reuse_proposal,
        SceneLayer(id="reused-ip-layer", source_id=first.id, name="Front in content"),
    )
    second = replace(first, id="second-ip", name="Separate connection")
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, second,
        SceneLayer(id="second-ip-layer", source_id=second.id, name="Separate"),
    )

    assert reused.source_id == first.id
    assert workspace.configured_cameras == (first, second)
    assert workspace.documents.document.source(first.id) == first


@pytest.mark.parametrize("add_to_document", [False, True])
@pytest.mark.parametrize("existing_in_document", [False, True])
def test_camera_alias_is_rejected_before_persisting_shared_resources(
    scene_workspace_factory, tmp_path: Path, add_to_document: bool,
    existing_in_document: bool, monkeypatch,
) -> None:
    monkeypatch.setattr(camera_identity, "sys", SimpleNamespace(platform="win32"))
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = replace(
        create_default_scene_document(_names(), document_id="fixture").source(DEFAULT_CAMERA_SOURCE_ID),
        configuration=LocalCameraConfig(device_id="physical-camera-1"),
    )
    workspace.upsert_camera(camera, add_to_active_document=existing_in_document)
    before_resources = workspace.resources
    before_document = workspace.documents.document
    stored = paths.scene_resources_file.read_bytes()
    events = []
    unsubscribe = workspace.subscribe(events.append)
    try:
        with pytest.raises(SceneValidationError, match="local camera device"):
            workspace.upsert_camera(
                replace(camera, id="camera-alias", configuration=LocalCameraConfig(device_id="PHYSICAL-CAMERA-1")),
                add_to_active_document=add_to_document,
            )
    finally:
        unsubscribe()

    assert workspace.resources == before_resources
    assert workspace.documents.document == before_document
    assert paths.scene_resources_file.read_bytes() == stored
    assert events == []


def test_camera_update_conflict_is_rejected_before_persisting_shared_resources(
    scene_workspace_factory, tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = replace(
        create_default_scene_document(_names(), document_id="fixture").source(DEFAULT_CAMERA_SOURCE_ID),
        configuration=LocalCameraConfig(device_id="physical-camera-1"),
    )
    workspace.upsert_camera(camera, add_to_active_document=True)
    second = replace(camera, id="second-camera", configuration=LocalCameraConfig(device_id="physical-camera-2"))
    workspace.upsert_camera(second, add_to_active_document=True)
    before_resources = workspace.resources
    before_document = workspace.documents.document
    stored = paths.scene_resources_file.read_bytes()

    with pytest.raises(SceneValidationError, match="local camera device"):
        workspace.upsert_camera(replace(second, configuration=camera.configuration))

    assert workspace.resources == before_resources
    assert workspace.documents.document == before_document
    assert paths.scene_resources_file.read_bytes() == stored


def test_invalid_active_camera_replacement_is_rejected_before_resource_save(
    scene_workspace_factory, tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = replace(
        create_default_scene_document(_names(), document_id="fixture").source(DEFAULT_CAMERA_SOURCE_ID),
        configuration=LocalCameraConfig(device_id="physical-camera-1"),
    )
    workspace.upsert_camera(camera)
    before_resources = workspace.resources
    before_document = workspace.documents.document
    stored = paths.scene_resources_file.read_bytes()

    with pytest.raises(SceneValidationError, match="no-signal fallback source"):
        workspace.upsert_camera(replace(
            camera, id=NO_SIGNAL_SOURCE_ID,
            configuration=LocalCameraConfig(device_id="physical-camera-2"),
        ))

    assert workspace.resources == before_resources
    assert workspace.documents.document == before_document
    assert paths.scene_resources_file.read_bytes() == stored


@pytest.mark.parametrize("survivor_owns_ptz", [False, True])
def test_orphaned_camera_gc_preserves_credentials_shared_with_surviving_camera(
    scene_workspace_factory, tmp_path, survivor_owns_ptz,
) -> None:
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    shared_ref = "shared-camera-credential"
    survivor_ref = "survivor-only-credential"
    orphan_ref = "orphan-only-credential"
    survivor = SourceDefinition(
        id="surviving-ip", kind=SourceKind.RTSP_CAMERA, name="Surviving camera",
        credential_ref=survivor_ref if survivor_owns_ptz else shared_ref,
        configuration=RtspCameraConfig(
            uri="rtsp://survivor.invalid/live",
            ptz_binding=OnvifPtzBinding(
                endpoint="http://survivor.invalid/onvif/device_service",
                credential_ref=shared_ref if survivor_owns_ptz else survivor_ref,
            ),
        ),
    )
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, survivor,
        SceneLayer(id="surviving-layer", source_id=survivor.id, name="Live camera"),
    )
    orphan = SourceDefinition(
        id="orphaned-ip", kind=SourceKind.RTSP_CAMERA, name="Orphaned camera",
        credential_ref=shared_ref if survivor_owns_ptz else orphan_ref,
        configuration=RtspCameraConfig(
            uri="rtsp://orphan.invalid/live",
            ptz_binding=OnvifPtzBinding(
                endpoint="http://orphan.invalid/onvif/device_service",
                credential_ref=orphan_ref if survivor_owns_ptz else shared_ref,
            ),
        ),
    )
    workspace.upsert_camera(orphan)
    deleted_credentials = []

    assert workspace.collect_orphaned_cameras() == (orphan,)

    assert workspace.configured_cameras == (survivor,)
    assert workspace.documents.document.source(survivor.id) == survivor
    assert workspace.resources.credential_references == frozenset((shared_ref, survivor_ref))
    assert workspace.resources.pending_credential_deletions == (orphan_ref,)
    assert workspace._resource_repository.load() == workspace.resources

    workspace.set_credential_cleaner(deleted_credentials.append)

    assert deleted_credentials == [orphan_ref]
    assert workspace.resources.pending_credential_deletions == ()
    assert workspace._resource_repository.load() == workspace.resources


def test_pending_credential_cleanup_skips_live_stream_and_ptz_references(
    scene_workspace_factory, tmp_path,
) -> None:
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    camera = SourceDefinition(
        id="live-ip", kind=SourceKind.RTSP_CAMERA, name="Live camera",
        credential_ref="live-stream-credential",
        configuration=RtspCameraConfig(
            uri="rtsp://camera.invalid/live",
            ptz_binding=OnvifPtzBinding(
                endpoint="http://camera.invalid/onvif/device_service",
                credential_ref="live-ptz-credential",
            ),
        ),
    )
    workspace.add_camera_layer(
        CAMERA_SCENE_ID, camera,
        SceneLayer(id="live-layer", source_id=camera.id, name="Live camera"),
    )
    workspace._save_resources(replace(
        workspace.resources, revision=workspace.resources.revision + 1,
        pending_credential_deletions=(
            "live-stream-credential", "orphan-credential", "live-ptz-credential",
        ),
    ))
    deleted_credentials = []

    workspace.set_credential_cleaner(deleted_credentials.append)

    assert deleted_credentials == ["orphan-credential"]
    assert workspace.configured_cameras == (camera,)
    assert workspace.resources.credential_references == frozenset((
        "live-stream-credential", "live-ptz-credential",
    ))
    assert workspace.resources.pending_credential_deletions == ()
    assert workspace._resource_repository.load() == workspace.resources


def test_orphaned_camera_cleanup_waits_for_backup_and_retries_credentials(
    scene_workspace_factory,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    camera = create_default_scene_document(_names(), document_id="camera-fixture").source(DEFAULT_CAMERA_SOURCE_ID)
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
    workspace.documents.create_source(workspace.configured_cameras[0])
    workspace.documents.add_layer(CAMERA_SCENE_ID, SceneLayer(id="camera-layer", source_id=camera.id, name="Camera"))
    camera_layer = workspace.documents.document.scene(CAMERA_SCENE_ID).layers[0]

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
    camera = create_default_scene_document(_names(), document_id="camera-fixture").source(DEFAULT_CAMERA_SOURCE_ID)
    workspace.upsert_camera(
        replace(
            camera,
            configuration=LocalCameraConfig(
                device_id="physical-camera-1",
                ptz_binding=OnvifPtzBinding(
                    endpoint="http://192.0.2.10/onvif/device_service"
                )
            ),
        ),
        add_to_active_document=True,
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
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers[0].fit_mode is FitMode.CONTAIN
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


def test_fresh_camera_is_retained_empty_without_a_configured_device(scene_workspace_factory, tmp_path):
    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
    assert workspace.configured_cameras == ()
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID
    other = workspace.create_collection("Empty Camera")
    workspace.activate_collection(other.id)
    assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID


def test_profile_activation_restores_independent_bases_and_preserves_enabled_outputs(scene_workspace_factory, tmp_path):
    from solin.core.scenes.model import ContentCategory, OutputMode

    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    first_id = workspace.active_collection.id
    workspace.documents.create_scene("A")
    extra = workspace.documents.document.scenes[-1]
    workspace.runtime.select_scene(BusId.MEDIA_WINDOWS, extra.id)
    workspace.runtime.take_scene(BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID)
    workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, True)
    other = workspace.create_collection("Other")
    workspace.activate_collection(other.id)
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).enabled
    workspace.runtime.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    workspace.runtime.set_output_enabled(BusId.MEDIA_WINDOWS, False)
    workspace.activate_collection(first_id)
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == extra.id
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CONTENT_SCENE_ID
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).mode is OutputMode.MANUAL
    assert not any(output.enabled for output in workspace.runtime.state.outputs)
    # Automatic media is temporary: resolving it never persists it as the base.
    assert workspace.runtime.resolve_scene(BusId.MEDIA_WINDOWS, ContentCategory.VIDEO) == CONTENT_SCENE_ID
    workspace.close()
    restarted = scene_workspace_factory(paths, seed_names=_names())
    assert restarted.runtime.resolve_scene(BusId.MEDIA_WINDOWS, ContentCategory.IDLE) == extra.id
    restarted.activate_collection(other.id)
    assert restarted.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CAMERA_SCENE_ID
    assert not any(output.enabled for output in restarted.runtime.state.outputs)


@pytest.mark.parametrize("mutation", ["name", "geometry", "duplicate", "disabled", "route"])
def test_pristine_fingerprint_rejects_every_customization(mutation):
    from solin.core.scenes.model import SceneDocument
    from solin.core.scenes.presets import convert_pristine_beta_preset

    raw = _beta_preset_record()
    if mutation == "name":
        raw["scenes"][1]["name"] = "My Camera"
    elif mutation == "geometry":
        raw["scenes"][2]["layers"][0]["rect"]["width"] = 0.9
    elif mutation == "duplicate":
        raw["scenes"][0]["layers"].append(dict(raw["scenes"][0]["layers"][0], id="extra"))
    elif mutation == "disabled":
        raw["sources"][0]["enabled"] = False
    else:
        raw["outputs"][0]["default_scene_id"] = CAMERA_SCENE_ID
    document = SceneDocument.from_record(raw)
    selections = {BusId(route["bus_id"]): route["default_scene_id"] for route in raw["outputs"]}
    assert convert_pristine_beta_preset(document, _names(), selections=selections) is document


@pytest.mark.parametrize("customized", [False, True])
@pytest.mark.parametrize("corrupt_primary", [False, True])
def test_existing_beta_catalog_without_resources_captures_routes_before_normalization(scene_workspace_factory, tmp_path, customized, corrupt_primary):
    from solin.core.scenes.collections import SceneCollection, SceneCollectionCatalog

    paths = _paths(tmp_path)
    catalog = SceneCollectionCatalog(revision=0, active_collection_id="old-beta", collections=(SceneCollection(id="old-beta", name="Beta"),))
    paths.scene_profiles_catalog_file.write_text(json.dumps(catalog.to_record()), encoding="utf-8")
    raw = _beta_preset_record(customized=customized)
    primary = paths.scene_profiles_dir / "old-beta.json"
    backup = paths.scene_profiles_dir / "old-beta.backup.json"
    primary.write_text("{corrupt" if corrupt_primary else json.dumps(raw), encoding="utf-8")
    backup.write_text(json.dumps(raw), encoding="utf-8")
    old_runtime = {
        "schema_version": 3, "document_id": raw["document_id"], "revision": 8,
        "outputs": [{"bus_id": bus.value, "mode": "auto", "manual_scene_id": "", "enabled": bus is BusId.MEDIA_WINDOWS} for bus in (BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA)],
    }
    paths.scenes_runtime_file.write_text(json.dumps(old_runtime), encoding="utf-8")
    assert not paths.scene_resources_file.exists()
    workspace = scene_workspace_factory(paths, seed_names=replace(_names(), content_source="Solin Content", content_scene="Media", content_layer="Solin Content"))
    assert workspace.runtime.state.revision == 8
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).enabled
    if customized:
        assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CAMERA_SCENE_ID
        assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == raw["scenes"][0]["id"]
        assert workspace.documents.document.scenes[0].name == "My idle"
    else:
        assert [scene.name for scene in workspace.documents.document.scenes] == ["Media", "Camera"]
        assert workspace.documents.document.scene(CAMERA_SCENE_ID).layers == ()
        assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID
        assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID
    for path in (primary, backup):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["schema_version"] == 13
        assert all("default_scene_id" not in output for output in record["outputs"])
        if not customized:
            assert [scene["name"] for scene in record["scenes"]] == ["Media", "Camera"]
    before = workspace.documents.document
    workspace.close()
    assert scene_workspace_factory(paths, seed_names=_names()).documents.document == before


@pytest.mark.parametrize("three_scene", [False, True])
@pytest.mark.parametrize("portuguese", [False, True])
def test_actual_shipped_beta_labels_convert_to_new_labels(three_scene, portuguese):
    from solin.core.scenes.model import SceneDocument
    from solin.core.scenes.presets import convert_pristine_beta_preset

    # Actual previous bootstrap labels, rather than the new Media/Solin Content seed.
    raw = _beta_preset_record()
    if not three_scene:
        raw["scenes"] = [raw["scenes"][1], raw["scenes"][2]]
        raw["sources"] = raw["sources"][:-1]
        for route in raw["outputs"]:
            route["default_scene_id"] = CAMERA_SCENE_ID
    if portuguese:
        translations = {"Idle screen": "Tela de descanso"}
        for scene in raw["scenes"]:
            scene["name"] = translations.get(scene["name"], scene["name"])
            for layer in scene["layers"]:
                layer["name"] = translations.get(layer["name"], layer["name"])
        for source in raw["sources"]:
            source["name"] = translations.get(source["name"], source["name"])
    document = SceneDocument.from_record(raw)
    selections = {BusId(route["bus_id"]): route["default_scene_id"] for route in raw["outputs"]}
    modern_names = replace(_names(), content_source="Solin Content", content_scene="Media", content_layer="Solin Content")
    converted = convert_pristine_beta_preset(document, modern_names, selections=selections)
    assert converted is not document
    assert [scene.name for scene in converted.scenes] == ["Media", "Camera"]
    assert converted.source(CONTENT_SOURCE_ID).name == "Solin Content"
    assert converted.scene(CONTENT_SCENE_ID).layers[0].name == "Solin Content"


def test_deleting_profiles_does_not_accumulate_runtime_states(scene_workspace_factory, tmp_path):
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    first_id = workspace.active_collection.id
    document_id = workspace.documents.document.document_id
    for index in range(6):
        other = workspace.create_collection(f"Temporary {index}")
        workspace.activate_collection(other.id)
        workspace.runtime.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
        workspace.activate_collection(first_id)
        workspace.delete_collection(other.id)
    record = json.loads(paths.scenes_runtime_file.read_text(encoding="utf-8"))
    assert [state["document_id"] for state in record["states"]] == [document_id]
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID


def test_profile_activation_preserves_active_automation_preference_per_output(scene_workspace_factory, tmp_path):
    from solin.core.scenes.model import OutputMode

    workspace = scene_workspace_factory(_paths(tmp_path), seed_names=_names())
    first_id = workspace.active_collection.id
    workspace.runtime.take_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    other = workspace.create_collection("Other")
    workspace.activate_collection(other.id)
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).mode is OutputMode.MANUAL
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).mode is OutputMode.AUTO
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CONTENT_SCENE_ID
    workspace.runtime.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    workspace.runtime.take_scene(BusId.VIRTUAL_CAMERA, CONTENT_SCENE_ID)
    workspace.activate_collection(first_id)
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).mode is OutputMode.AUTO
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).mode is OutputMode.MANUAL
    assert workspace.runtime.state.output(BusId.MEDIA_WINDOWS).base_scene_id == CAMERA_SCENE_ID
    assert workspace.runtime.state.output(BusId.VIRTUAL_CAMERA).base_scene_id == CAMERA_SCENE_ID


def test_failed_runtime_cleanup_keeps_collection_and_files(scene_workspace_factory, tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    other = workspace.create_collection("Other")

    def fail_delete(_document_id):
        raise OSError("runtime cleanup failed")

    monkeypatch.setattr(workspace._runtime_repository, "delete", fail_delete)
    with pytest.raises(OSError, match="runtime cleanup failed"):
        workspace.delete_collection(other.id)
    assert workspace.catalog.collection(other.id) == other
    assert (paths.scene_profiles_dir / f"{other.id}.json").exists()


@pytest.mark.parametrize(("idle_name", "media_name", "content_name"), [
    ("Ruhebild", "Medien", "Solin-Inhalt"),
    ("\u30a2\u30a4\u30c9\u30eb\u753b\u9762", "\u30e1\u30c7\u30a3\u30a2", "Solin\u30b3\u30f3\u30c6\u30f3\u30c4"),
])
def test_shipped_german_and_japanese_beta_presets_keep_new_localized_labels(idle_name, media_name, content_name):
    from solin.core.scenes.model import SceneDocument
    from solin.core.scenes.presets import convert_pristine_beta_preset

    raw = _beta_preset_record()
    raw["sources"][-1]["name"] = idle_name
    raw["scenes"][0]["layers"][0]["name"] = idle_name
    document = SceneDocument.from_record(raw)
    selections = {BusId(route["bus_id"]): route["default_scene_id"] for route in raw["outputs"]}
    names = replace(_names(), content_source=content_name, content_scene=media_name, content_layer=content_name)
    converted = convert_pristine_beta_preset(document, names, selections=selections)
    assert converted is not document
    assert converted.scene(CONTENT_SCENE_ID).name == media_name
    assert converted.source(CONTENT_SOURCE_ID).name == content_name
    assert converted.scene(CONTENT_SCENE_ID).layers[0].name == content_name
