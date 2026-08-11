from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from solin.core.scenes.application import SceneDocumentService
from solin.core.scenes.model import SceneDocument, UnsupportedSceneSchemaError
from solin.core.scenes.presets import (
    CONTENT_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)
from solin.core.scenes.repository import (
    SceneDocumentRepository,
    SceneRepositoryCorruptError,
    SceneRevisionConflictError,
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
