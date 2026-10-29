from __future__ import annotations

import hashlib
import io
import json
import subprocess

import pytest

from scripts import obs_sources as sources_build


def test_source_download_requires_checksum_and_never_publishes_invalid_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sources_build.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"wrong")
    )
    with pytest.raises(sources_build.ObsSourceError, match="SHA-256"):
        sources_build.download_sources(tmp_path)
    assert not list(tmp_path.iterdir())


def test_existing_source_archive_is_verified_before_reuse(tmp_path, monkeypatch):
    content = b"verified OBS archive"
    archive = tmp_path / f"obs-studio-{sources_build.OBS_VERSION}.tar.gz"
    archive.write_bytes(content)
    monkeypatch.setattr(sources_build, "SOURCE_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(
        sources_build.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("cache miss")
    )
    assert sources_build.download_sources(tmp_path) == archive
    archive.write_bytes(b"corrupted")
    with pytest.raises(sources_build.ObsSourceError, match="SHA-256"):
        sources_build.download_sources(tmp_path)


@pytest.fixture
def patch_project(tmp_path):
    directory = tmp_path / "native"
    patches = directory / "patches"
    patches.mkdir(parents=True)
    (patches / "series.json").write_text(
        json.dumps(
            [{"file": "fix.patch", "purpose": "Correct the fixture", "upstream": "candidate"}]
        )
    )
    (patches / "fix.patch").write_text(
        "--- a/media.c\n+++ b/media.c\n@@ -1 +1 @@\n-old\n+corrected\n"
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "media.c").write_text("old\n")
    return sources, patches


def test_unified_patch_applies_to_the_expected_upstream_context(patch_project):
    sources, _patches = patch_project
    sources_build.apply_patches(sources, _patches)
    assert (sources / "media.c").read_text() == "corrected\n"


def test_git_format_patch_inside_checkout_cannot_silently_skip_files(patch_project):
    sources, patches = patch_project
    subprocess.run(["git", "init", "--quiet", str(sources.parent)], check=True)
    patch = patches / "fix.patch"
    patch.write_text("diff --git a/media.c b/media.c\n" + patch.read_text())
    sources_build.apply_patches(sources, patches)
    assert (sources / "media.c").read_text() == "corrected\n"


def test_patch_context_mismatch_fails_without_changing_sources(patch_project):
    sources, _patches = patch_project
    (sources / "media.c").write_text("upstream changed\n")
    with pytest.raises(sources_build.ObsSourceError, match="no longer applies"):
        sources_build.apply_patches(sources, _patches)
    assert (sources / "media.c").read_text() == "upstream changed\n"


@pytest.mark.parametrize("name", ["../fix.patch", "nested/fix.patch", "C:\\fix.patch", "fix.txt"])
def test_patch_series_rejects_invalid_paths(patch_project, name):
    _sources, patches = patch_project
    entry = {"file": name, "purpose": "fix", "upstream": "candidate"}
    (patches / "series.json").write_text(json.dumps([entry]))
    with pytest.raises(sources_build.ObsSourceError, match="patch path"):
        sources_build.patch_series(patches)


def test_patch_series_requires_unique_files(patch_project):
    _sources, patches = patch_project
    entry = {"file": "fix.patch", "purpose": "fix", "upstream": "candidate"}
    (patches / "series.json").write_text(json.dumps([entry, entry]))
    with pytest.raises(sources_build.ObsSourceError, match="duplicate"):
        sources_build.patch_series(patches)


def test_patch_series_requires_prerequisites_before_dependents(patch_project):
    _sources, patches = patch_project
    entry = {
        "file": "fix.patch",
        "purpose": "fix",
        "upstream": "candidate",
        "requires": ["missing.patch"],
    }
    (patches / "series.json").write_text(json.dumps([entry]))
    with pytest.raises(sources_build.ObsSourceError, match="out of order"):
        sources_build.patch_series(patches)
