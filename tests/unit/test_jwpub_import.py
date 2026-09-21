from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from solin.core.jw import jwpub_import, jwpub_import_thread
from solin.core.jw.publication_links import PubMediaFile
from tests._jwpub_fixture import build_synthetic_jwpub


@pytest.fixture(scope="module")
def synthetic_jwpub_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    fixture_dir = tmp_path_factory.mktemp("synthetic-jwpub")
    return build_synthetic_jwpub(fixture_dir / "synthetic_meeting_workbook.jwpub")


def _track_extraction_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    extraction_dirs: list[Path] = []
    real_temporary_directory = TemporaryDirectory

    def create_tracked_directory(*args, **kwargs):
        directory = real_temporary_directory(*args, **kwargs)
        extraction_dirs.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        jwpub_import.tempfile,
        "TemporaryDirectory",
        create_tracked_directory,
    )
    return extraction_dirs


def test_jwpub_reader_persists_images_and_removes_extraction_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_jwpub_fixture: Path,
):
    extraction_dirs = _track_extraction_dirs(monkeypatch)
    destination = tmp_path / "images"

    items, _stem = jwpub_import.JwpubPlaylistImportService().read(
        jwpub_import.JwpubImportRequest(
            jwpub_path=str(synthetic_jwpub_fixture),
            dest_images_dir=str(destination),
            resolve_urls=False,
        )
    )

    image_paths = [Path(item["url"]) for item in items if item["type"] == "image"]
    assert image_paths
    assert [item["source_item_id"] for item in items if item["type"] == "image"] == ["4"]
    assert [item["source_item_id"] for item in items if item["type"] == "video"] == ["5"]
    assert all(path.parent == destination and path.is_file() for path in image_paths)
    assert extraction_dirs
    assert all(not path.exists() for path in extraction_dirs)


def test_jwpub_reader_removes_extraction_dir_when_image_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_jwpub_fixture: Path,
):
    extraction_dirs = _track_extraction_dirs(monkeypatch)

    def fail_copy(*_args, **_kwargs):
        raise PermissionError("destination is not writable")

    monkeypatch.setattr(jwpub_import.shutil, "copy2", fail_copy)

    with pytest.raises(PermissionError):
        jwpub_import.JwpubPlaylistImportService().read(
            jwpub_import.JwpubImportRequest(
                jwpub_path=str(synthetic_jwpub_fixture),
                dest_images_dir=str(tmp_path / "images"),
                resolve_urls=False,
            )
        )

    assert extraction_dirs
    assert all(not path.exists() for path in extraction_dirs)


def test_jwpub_import_service_uses_injected_media_resolver(
    tmp_path: Path,
    synthetic_jwpub_fixture: Path,
):
    calls = []

    def resolve_media(key_symbol, track, issue_tag, meps_doc_id, language):
        calls.append((key_symbol, track, issue_tag, meps_doc_id, language))
        return PubMediaFile(
            url=f"https://cdn.example/{key_symbol or meps_doc_id}.mp4",
            title="Resolved media",
        )

    service = jwpub_import.JwpubPlaylistImportService(resolve_media=resolve_media)

    items, _stem = service.read(
        jwpub_import.JwpubImportRequest(
            jwpub_path=str(synthetic_jwpub_fixture),
            language="T",
            dest_images_dir=str(tmp_path / "images"),
            resolve_urls=True,
        )
    )

    video_items = [item for item in items if item["type"] in {"video", "audio"}]
    assert calls
    assert video_items
    assert all(item["url"].startswith("https://cdn.example/") for item in video_items)
    assert all(item["title"] == "Resolved media" for item in video_items)


def test_jwpub_import_thread_factory_uses_default_and_override_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    default_destination = tmp_path / "profile-images"
    override_destination = tmp_path / "linked-folder-cache"
    parent = object()
    threads = [object(), object()]

    monkeypatch.setattr(
        jwpub_import_thread.JwpubImportThread,
        "create",
        lambda path, *, lang, dest_images_dir, service, parent: calls.append(
            (path, lang, dest_images_dir, service, parent)
        )
        or threads.pop(0),
    )

    service = jwpub_import.JwpubPlaylistImportService()
    factory = jwpub_import_thread.JwpubImportThreadFactory(
        default_destination,
        service=service,
    )

    first = factory.create("first.jwpub", lang="T", parent=parent)
    second = factory.create(
        "second.jwpub",
        lang="E",
        dest_images_dir=override_destination,
        parent=parent,
    )

    assert first is not second
    assert calls == [
        ("first.jwpub", "T", str(default_destination), service, parent),
        ("second.jwpub", "E", str(override_destination), service, parent),
    ]
