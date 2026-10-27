from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile

import pytest

from scripts import build_media_playback as builder


def test_source_download_requires_checksum_and_never_publishes_invalid_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"wrong"))
    with pytest.raises(builder.MediaPlaybackBuildError, match="SHA-256"):
        builder.download_sources(tmp_path)
    assert not list(tmp_path.iterdir())


def test_existing_source_archive_is_verified_before_reuse(tmp_path, monkeypatch):
    content = b"verified OBS archive"
    archive = tmp_path / f"obs-studio-{builder.OBS_VERSION}.tar.gz"
    archive.write_bytes(content)
    monkeypatch.setattr(builder, "SOURCE_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("cache miss")
    )
    assert builder.download_sources(tmp_path) == archive
    archive.write_bytes(b"corrupted")
    with pytest.raises(builder.MediaPlaybackBuildError, match="SHA-256"):
        builder.download_sources(tmp_path)


@pytest.fixture
def patch_project(tmp_path, monkeypatch):
    directory = tmp_path / "native"
    patches = directory / "patches"
    patches.mkdir(parents=True)
    (patches / "series.json").write_text(json.dumps([
        {"file": "fix.patch", "purpose": "Correct the fixture", "upstream": "candidate"}
    ]))
    (patches / "fix.patch").write_text(
        "--- a/media.c\n+++ b/media.c\n@@ -1 +1 @@\n-old\n+corrected\n"
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "media.c").write_text("old\n")
    monkeypatch.setattr(builder, "SOURCE_DIRECTORY", directory)
    return sources, patches


def test_unified_patch_applies_to_the_expected_upstream_context(patch_project):
    sources, _patches = patch_project
    builder.apply_patches(sources)
    assert (sources / "media.c").read_text() == "corrected\n"


def test_git_format_patch_inside_checkout_cannot_silently_skip_files(patch_project):
    sources, patches = patch_project
    subprocess.run(["git", "init", "--quiet", str(sources.parent)], check=True)
    patch = patches / "fix.patch"
    patch.write_text("diff --git a/media.c b/media.c\n" + patch.read_text())
    builder.apply_patches(sources)
    assert (sources / "media.c").read_text() == "corrected\n"


def test_patch_context_mismatch_fails_without_changing_sources(patch_project):
    sources, _patches = patch_project
    (sources / "media.c").write_text("upstream changed\n")
    with pytest.raises(builder.MediaPlaybackBuildError, match="no longer applies"):
        builder.apply_patches(sources)
    assert (sources / "media.c").read_text() == "upstream changed\n"


@pytest.mark.parametrize("name", ["../fix.patch", "nested/fix.patch", "C:\\fix.patch", "fix.txt"])
def test_patch_series_rejects_invalid_paths(patch_project, name):
    _sources, patches = patch_project
    entry = {"file": name, "purpose": "fix", "upstream": "candidate"}
    (patches / "series.json").write_text(json.dumps([entry]))
    with pytest.raises(builder.MediaPlaybackBuildError, match="patch path"):
        builder.patch_series()


def test_patch_series_requires_unique_files(patch_project):
    _sources, patches = patch_project
    entry = {"file": "fix.patch", "purpose": "fix", "upstream": "candidate"}
    (patches / "series.json").write_text(json.dumps([entry, entry]))
    with pytest.raises(builder.MediaPlaybackBuildError, match="duplicate"):
        builder.patch_series()


def test_patch_series_requires_prerequisites_before_dependents(patch_project):
    _sources, patches = patch_project
    entry = {"file": "fix.patch", "purpose": "fix", "upstream": "candidate",
             "requires": ["missing.patch"]}
    (patches / "series.json").write_text(json.dumps([entry]))
    with pytest.raises(builder.MediaPlaybackBuildError, match="out of order"):
        builder.patch_series()


@pytest.mark.parametrize("corrupted", [False, True])
def test_sdk_cache_reuse_checks_headers_against_verified_archive(tmp_path, monkeypatch, corrupted):
    sdk = tmp_path / "sdk-windows-x86_64"
    header = sdk / "include/libavcodec/avcodec.h"
    header.parent.mkdir(parents=True)
    header.write_bytes(b"corrupted" if corrupted else b"verified header")
    archive = tmp_path / "sdk.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("include/libavcodec/avcodec.h", b"verified header")
    monkeypatch.setattr(builder, "_download_sdk", lambda *_args: archive)
    rebuilt = []
    monkeypatch.setattr(builder, "prepare_sdk", lambda *_args: rebuilt.append(True) or sdk)
    assert builder.prepare_playback_sdk("windows", "x86_64", tmp_path) == sdk
    assert bool(rebuilt) == corrupted


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("identical", [False, True])
def test_install_qualifies_candidate_before_publishing_module_and_receipt(
    tmp_path, monkeypatch, failed, identical
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plugins = runtime / "obs-plugins"
    plugins.mkdir()
    binary = tmp_path / "solin-media-source.dll"
    binary.write_bytes(b"new compiled module")
    installed = plugins / binary.name
    previous = binary.read_bytes() if identical else b"previous module"
    installed.write_bytes(previous)
    unrelated = plugins / "obs-ffmpeg.dll"
    unrelated.write_bytes(b"original upstream module")
    sources = tmp_path / "sources"
    locale = sources / "plugins/obs-ffmpeg/data/locale/en-US.ini"
    locale.parent.mkdir(parents=True)
    locale.write_text("upstream locale")
    (sources / "COPYING").write_text("upstream license")

    def qualify(candidate, data):
        assert candidate.read_bytes() == binary.read_bytes()
        assert installed.read_bytes() == previous
        assert (data / "locale/en-US.ini").is_file()
        assert (data / "COPYING").is_file()
        if failed:
            raise builder.MediaPlaybackBuildError("qualification failed")

    monkeypatch.setattr(builder, "verify_module", qualify)
    replace = builder.os.replace

    def replace_unlocked(source, destination):
        if destination == installed and identical:
            pytest.fail("An identical loaded module must not be replaced")
        return replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", replace_unlocked)
    if failed:
        with pytest.raises(builder.MediaPlaybackBuildError, match="qualification"):
            builder.install_module(binary, sources, runtime, "windows")
        assert installed.read_bytes() == previous
        assert not (runtime / builder.MANIFEST_NAME).exists()
    else:
        assert builder.install_module(binary, sources, runtime, "windows") == installed
        receipt = json.loads((runtime / builder.MANIFEST_NAME).read_text())
        assert receipt["source_id"] == builder.SOURCE_ID
        assert receipt["source_sha256"] == builder.SOURCE_SHA256
        assert runtime / receipt["plugin"]["path"] == installed
        assert receipt["plugin"]["sha256"] == hashlib.sha256(installed.read_bytes()).hexdigest()
        assert (runtime / "data/obs-plugins/solin-media-source/COPYING").is_file()
    assert unrelated.read_bytes() == b"original upstream module"
    assert not list(runtime.glob(".solin-media-*"))


def test_module_qualification_failure_includes_native_diagnostics(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *_a, **_k: CompletedProcess([], 3, "native output", "native error"),
    )
    with pytest.raises(builder.MediaPlaybackBuildError, match="native error"):
        builder.verify_module(tmp_path / "module.dll", tmp_path / "data")
