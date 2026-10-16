from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.model import (
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    RtspCameraConfig,
    SceneDocument,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    UnsupportedSceneSchemaError,
)
from solin.core.scenes.presets import (
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import (
    SceneCollectionDocumentRepository,
    SceneDocumentRepository,
    SceneResourceCatalogRepository,
    SceneRepositoryCorruptError,
    SceneRevisionConflictError,
)
from solin.core.scenes.resources import SceneResourceCatalog, UnsupportedSceneResourceSchemaError


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


def _repository(path: Path) -> SceneDocumentRepository:
    return SceneDocumentRepository(path, seed_factory=_document)


def test_load_or_create_materializes_one_stable_default_document(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenes.json"
    repository = _repository(path)

    with pytest.raises(FileNotFoundError):
        repository.load()

    created = repository.load_or_create()

    assert created == _document()
    assert repository.load_or_create() == created
    assert path.exists()
    assert not repository.backup_path.exists()


def test_save_is_atomic_and_keeps_the_previous_valid_revision_as_backup(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "scenes.json")
    original = _document()
    repository.save(original, expected_revision=0)
    service = SceneDocumentService(
        original,
        clock=lambda: "2026-08-02T13:00:00+00:00",
    )
    updated = service.rename_scene(CONTENT_SCENE_ID, "Updated content")

    repository.save(updated, expected_revision=0)

    assert repository.load() == updated
    backup = repository.load_backup()
    assert replace(backup, revision=original.revision) == original
    assert backup.revision == updated.revision + 1
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_stale_writer_cannot_overwrite_a_newer_scene_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "scenes.json")
    original = _document()
    repository.save(original)
    updated = SceneDocumentService(original).rename_scene(
        CONTENT_SCENE_ID,
        "Updated content",
    )
    repository.save(updated, expected_revision=0)

    with pytest.raises(SceneRevisionConflictError, match="Expected scene revision"):
        repository.save(original, expected_revision=0)

    assert repository.load() == updated


def test_corrupt_primary_is_never_silently_overwritten(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "scenes.json")
    original = _document()
    repository.save(original)
    corrupt_bytes = b'{"schema_version": 1, broken'
    repository.path.write_bytes(corrupt_bytes)
    updated = SceneDocumentService(original).rename_scene(
        CONTENT_SCENE_ID,
        "Updated content",
    )

    with pytest.raises(SceneRepositoryCorruptError):
        repository.load()
    with pytest.raises(SceneRepositoryCorruptError):
        repository.save(updated, expected_revision=0)

    assert repository.path.read_bytes() == corrupt_bytes


def test_explicit_backup_recovery_replaces_a_corrupt_primary(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "scenes.json")
    original = _document()
    repository.save(original)
    updated = SceneDocumentService(original).rename_scene(
        CONTENT_SCENE_ID,
        "Updated content",
    )
    repository.save(updated, expected_revision=0)
    repository.path.write_text("not json", encoding="utf-8")

    recovered = repository.recover_from_backup()

    assert replace(recovered, revision=original.revision) == original
    assert recovered.revision > updated.revision
    assert repository.load() == recovered


def test_future_schema_is_preserved_as_an_explicit_unsupported_version(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "scenes.json")
    repository.path.write_text(
        '{"schema_version": 999}',
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedSceneSchemaError) as error:
        repository.load()

    assert error.value.version == 999
    assert repository.path.read_text(encoding="utf-8") == '{"schema_version": 999}'


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "scenes.json")
    repository.path.write_text(
        '{"schema_version": 1, "schema_version": 1}',
        encoding="utf-8",
    )

    with pytest.raises(SceneRepositoryCorruptError):
        repository.load()


def test_scene_service_persists_with_keyword_only_optimistic_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "scenes.json")
    original = repository.load_or_create()
    service = SceneDocumentService(original, store=repository)

    updated = service.rename_scene(CONTENT_SCENE_ID, "Persisted")

    assert repository.load() == updated


def _camera(kind=SourceKind.LOCAL_CAMERA):
    return SourceDefinition(
        id=DEFAULT_CAMERA_SOURCE_ID,
        name="Camera",
        kind=kind,
        configuration=LocalCameraConfig(device_id="device")
        if kind is SourceKind.LOCAL_CAMERA
        else RtspCameraConfig(uri="rtsp://camera.local/live"),
    )


@pytest.mark.parametrize("kind", [SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA])
@pytest.mark.parametrize("flag", ["absent", True, False])
def test_resource_schema_one_migrates_and_normalizes_disk(tmp_path, kind, flag):
    path = tmp_path / "resources.json"
    expected = SceneResourceCatalog(
        revision=4, cameras=(_camera(kind),), pending_credential_deletions=("old-secret",)
    )
    raw = expected.to_record()
    raw["schema_version"] = 1
    if flag != "absent":
        raw["cameras"][0]["configuration"]["keep_active"] = flag
    original = deepcopy(raw)
    restored = SceneResourceCatalog.from_record(raw)
    assert restored.schema_version == 2
    assert restored == expected
    assert raw == original
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert SceneResourceCatalogRepository(path).load() == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected.to_record()


def test_equal_typed_resource_save_still_upgrades_disk_schema(tmp_path):
    path = tmp_path / "resources.json"
    expected = SceneResourceCatalog(revision=4, cameras=(_camera(),))
    raw = expected.to_record()
    raw["schema_version"] = 1
    raw["cameras"][0]["configuration"]["keep_active"] = False
    path.write_text(json.dumps(raw), encoding="utf-8")

    SceneResourceCatalogRepository(path).save(expected, expected_revision=4)

    assert json.loads(path.read_text(encoding="utf-8")) == expected.to_record()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "invalid",
    [{"keep_active": None}, {"keep_active": 1}, {"keep_active": "true"}, {"unexpected": True}],
)
def test_invalid_resource_data_is_rejected_and_preserved(tmp_path, version, invalid):
    path = tmp_path / "resources.json"
    resources = SceneResourceCatalog(revision=0, cameras=(_camera(),))
    raw = resources.to_record()
    raw["schema_version"] = version
    raw["cameras"][0]["configuration"].update(invalid)
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()
    repository = SceneResourceCatalogRepository(path)
    with pytest.raises(SceneRepositoryCorruptError):
        repository.load()
    with pytest.raises(SceneRepositoryCorruptError):
        repository.save(resources)
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [12, 13])
@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize(
    "invalid", ["flag", "unknown_config", "unknown_source", "invalid_config", "invalid_name"]
)
def test_collection_validates_inline_cameras_before_catalog_replacement(
    tmp_path, version, backup, invalid
):
    resources = SceneResourceCatalog(revision=0, cameras=(_camera(),))
    repository = SceneCollectionDocumentRepository(
        tmp_path / "collection.json", resources=lambda: resources
    )
    raw = _document().to_record()
    raw["schema_version"] = version
    camera = next(source for source in raw["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID)
    if invalid == "flag":
        camera["configuration"]["keep_active"] = "true" if version == 12 else True
    elif invalid == "unknown_config":
        camera["configuration"]["unexpected"] = True
    elif invalid == "unknown_source":
        camera["unexpected"] = True
    elif invalid == "invalid_config":
        camera["configuration"]["width"] = -1
    else:
        camera["name"] = ""
    path = repository.backup_path if backup else repository.path
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(SceneRepositoryCorruptError):
        (repository.load_backup if backup else repository.load)()

    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", [SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA])
@pytest.mark.parametrize("flag", ["absent", True, False])
def test_collection_normalizes_legacy_primary_and_backup_without_losing_enabled_state(
    tmp_path, kind, flag
):
    camera = _camera(kind)
    resources = SceneResourceCatalog(revision=0, cameras=(camera,))
    repository = SceneCollectionDocumentRepository(
        tmp_path / "collection.json", resources=lambda: resources
    )
    raw = _document().to_record()
    raw["schema_version"] = 12
    inline = next(item for item in raw["sources"] if item["id"] == camera.id)
    inline.update(camera.to_record())
    inline["enabled"] = False
    if flag != "absent":
        inline["configuration"]["keep_active"] = flag
    for path in (repository.path, repository.backup_path):
        path.write_text(json.dumps(raw), encoding="utf-8")

    assert not repository.load().source(camera.id).enabled
    assert not repository.load_backup().source(camera.id).enabled
    repository.normalize_storage()

    for path in (repository.path, repository.backup_path):
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["schema_version"] == 13
        assert next(item for item in saved["sources"] if item["id"] == camera.id) == {
            "id": camera.id,
            "type": "camera_ref",
            "enabled": False,
        }
        assert "keep_active" not in path.read_text(encoding="utf-8")
    assert not repository.load().source(camera.id).enabled


@pytest.mark.parametrize("operation", ["load", "save"])
def test_future_resource_schema_is_preserved_even_with_unknown_fields(tmp_path, operation):
    path = tmp_path / "resources.json"
    path.write_text('{"schema_version":999,"future_setting":true}', encoding="utf-8")
    original = path.read_bytes()
    repository = SceneResourceCatalogRepository(path)

    with pytest.raises(UnsupportedSceneResourceSchemaError) as error:
        repository.load() if operation == "load" else repository.save(
            SceneResourceCatalog(revision=0)
        )

    assert error.value.version == 999
    assert path.read_bytes() == original


@pytest.mark.parametrize("future_backup", [False, True])
@pytest.mark.parametrize("operation", ["load", "normalize", "recover"])
def test_future_collection_primary_or_backup_is_preserved(tmp_path, future_backup, operation):
    resources = SceneResourceCatalog(revision=0, cameras=(_camera(),))
    repository = SceneCollectionDocumentRepository(
        tmp_path / "collection.json", resources=lambda: resources
    )
    legacy = _document().to_record()
    legacy["schema_version"] = 12
    for path in (repository.path, repository.backup_path):
        path.write_text(json.dumps(legacy), encoding="utf-8")
    future_path = repository.backup_path if future_backup else repository.path
    future_path.write_text('{"schema_version":999,"future_setting":true}', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (repository.path, repository.backup_path)}

    with pytest.raises(UnsupportedSceneSchemaError):
        if operation == "load":
            (repository.load_backup if future_backup else repository.load)()
        elif operation == "normalize":
            repository.normalize_storage()
        else:
            repository.recover_from_backup()

    assert all(path.read_bytes() == original for path, original in originals.items())


@pytest.mark.parametrize("equal_save", [False, True])
def test_resource_migration_write_failure_preserves_original_disk_data(
    tmp_path, monkeypatch, equal_save
):
    path = tmp_path / "resources.json"
    resources = SceneResourceCatalog(revision=4, cameras=(_camera(),))
    record = resources.to_record()
    record["schema_version"] = 1
    record["cameras"][0]["configuration"]["keep_active"] = False
    path.write_text(json.dumps(record), encoding="utf-8")
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr("solin.core.storage.json_files.os.replace", fail_replace)
    repository = SceneResourceCatalogRepository(path)
    with pytest.raises(OSError, match="replacement failed"):
        repository.save(resources, expected_revision=4) if equal_save else repository.load()
    assert path.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "resources.json",
        "resources.json.lock",
    ]


def test_stale_resource_save_does_not_migrate_disk_as_a_side_effect(tmp_path):
    path = tmp_path / "resources.json"
    resources = SceneResourceCatalog(revision=4, cameras=(_camera(),))
    raw = resources.to_record()
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SceneRevisionConflictError):
        SceneResourceCatalogRepository(path).save(resources, expected_revision=3)
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [True, False, 1.0, 2.0, "1", None])
def test_resource_schema_requires_an_integer(version):
    raw = SceneResourceCatalog(revision=0).to_record()
    raw["schema_version"] = version
    with pytest.raises(SceneValidationError, match="schema version must be an integer"):
        SceneResourceCatalog.from_record(raw)


@pytest.mark.parametrize("flag", [True, False])
def test_current_resource_schema_forbids_boolean_legacy_flag(tmp_path, flag):
    path = tmp_path / "resources.json"
    raw = SceneResourceCatalog(revision=0, cameras=(_camera(),)).to_record()
    raw["cameras"][0]["configuration"]["keep_active"] = flag
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SceneRepositoryCorruptError):
        SceneResourceCatalogRepository(path).load()
    assert path.read_bytes() == original


@pytest.mark.parametrize("collection", [False, True])
def test_save_cannot_overwrite_a_future_backup(tmp_path, collection):
    resources = SceneResourceCatalog(revision=0, cameras=(_camera(),))
    path = tmp_path / "collection.json"
    repository = (
        SceneCollectionDocumentRepository(path, resources=lambda: resources)
        if collection
        else _repository(path)
    )
    original = _document()
    repository.save(original)
    repository.backup_path.write_text(
        '{"schema_version":999,"future_setting":true}', encoding="utf-8"
    )
    originals = {item: item.read_bytes() for item in (repository.path, repository.backup_path)}
    updated = replace(original, revision=1)

    with pytest.raises(UnsupportedSceneSchemaError):
        repository.save(updated, expected_revision=0)

    assert all(item.read_bytes() == data for item, data in originals.items())
