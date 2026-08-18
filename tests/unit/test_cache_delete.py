from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from solin.core.media import cache as cache_module
from solin.core.media.cache import remove_cached_entry
from solin.core.media.cache_delete import CacheDeletionSessionFactory


def _cached_file(cache_dir: Path, name: str) -> Path:
    path = cache_dir / name
    path.write_bytes(b"media")
    Path(f"{path}.done").write_text(
        f"https://cdn.example/{name}",
        encoding="utf-8",
    )
    return path


def test_remove_cached_entry_deletes_media_and_sidecar(tmp_path):
    cache_dir = tmp_path / "media"
    cache_dir.mkdir()
    path = _cached_file(cache_dir, "sample.mp4")

    assert remove_cached_entry(cache_dir, path) is True
    assert not path.exists()
    assert not Path(f"{path}.done").exists()


def test_remove_cached_entry_keeps_media_when_sidecar_removal_fails(
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "media"
    cache_dir.mkdir()
    path = _cached_file(cache_dir, "sample.mp4")
    sidecar = Path(f"{path}.done")
    sidecar_target = cache_module.os.path.normcase(
        cache_module.os.path.abspath(sidecar)
    )
    original_remove = cache_module.os.remove
    attempts: list[str] = []

    def _remove(target):
        attempts.append(target)
        if target == sidecar_target:
            raise PermissionError("sidecar is busy")
        original_remove(target)

    monkeypatch.setattr(cache_module.os, "remove", _remove)

    with pytest.raises(PermissionError, match="sidecar is busy"):
        remove_cached_entry(cache_dir, path)

    assert attempts == [sidecar_target]
    assert path.exists()
    assert sidecar.exists()


def test_remove_cached_entry_rejects_path_outside_cache(tmp_path):
    cache_dir = tmp_path / "media"
    cache_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep")

    with pytest.raises(ValueError, match="outside"):
        remove_cached_entry(cache_dir, outside)

    assert outside.read_bytes() == b"keep"


def test_remove_cached_entry_rejects_symlink(tmp_path):
    cache_dir = tmp_path / "media"
    cache_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep")
    link = cache_dir / "linked.mp4"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ValueError, match="regular files"):
        remove_cached_entry(cache_dir, link)

    assert outside.read_bytes() == b"keep"


def test_cache_deletion_session_reports_stable_paths(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    cache_dir = tmp_path / "media"
    cache_dir.mkdir()
    first = _cached_file(cache_dir, "first.mp4")
    second = _cached_file(cache_dir, "second.m4a")
    session = CacheDeletionSessionFactory().create(
        cache_dir,
        [str(first), str(second)],
    )
    completed: list[tuple[list[str], list[tuple[str, str]]]] = []
    loop = QEventLoop()
    session.completed.connect(lambda deleted, failures: completed.append((deleted, failures)))
    session.finished.connect(loop.quit)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(3_000)

    session.start()
    loop.exec()
    app.processEvents()

    assert completed == [([str(first), str(second)], [])]
    assert not first.exists()
    assert not second.exists()
