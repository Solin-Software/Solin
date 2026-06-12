from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.core.jw import publication_reader

_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_meeting_workbook.jwpub"


def _track_extraction_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    extraction_dirs: list[Path] = []
    real_temporary_directory = TemporaryDirectory

    def create_tracked_directory(*args, **kwargs):
        directory = real_temporary_directory(*args, **kwargs)
        extraction_dirs.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        publication_reader.tempfile,
        "TemporaryDirectory",
        create_tracked_directory,
    )
    return extraction_dirs


def test_jwpub_reader_persists_images_and_removes_extraction_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    extraction_dirs = _track_extraction_dirs(monkeypatch)
    destination = tmp_path / "images"

    items, _stem = publication_reader.read_jwpub_for_playlist(
        str(_FIXTURE),
        dest_images_dir=str(destination),
        resolve_urls=False,
    )

    image_paths = [Path(item["url"]) for item in items if item["type"] == "image"]
    assert image_paths
    assert all(path.parent == destination and path.is_file() for path in image_paths)
    assert extraction_dirs
    assert all(not path.exists() for path in extraction_dirs)


def test_jwpub_reader_removes_extraction_dir_when_image_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    extraction_dirs = _track_extraction_dirs(monkeypatch)

    def fail_copy(*_args, **_kwargs):
        raise PermissionError("destination is not writable")

    monkeypatch.setattr(publication_reader.shutil, "copy2", fail_copy)

    with pytest.raises(PermissionError):
        publication_reader.read_jwpub_for_playlist(
            str(_FIXTURE),
            dest_images_dir=str(tmp_path / "images"),
            resolve_urls=False,
        )

    assert extraction_dirs
    assert all(not path.exists() for path in extraction_dirs)
