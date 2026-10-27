from __future__ import annotations

import hashlib
import io
import json
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


@pytest.mark.parametrize(
    "name",
    [
        "plugins/obs-ffmpeg/obs-ffmpeg-source.c",
        "shared/media-playback/media-playback/decode.c",
        "shared/media-playback/media-playback/media.c",
        "shared/media-playback/media-playback/cache.c",
        "shared/media-playback/media-playback/media-playback.h",
        "shared/media-playback/media-playback/media.h",
    ],
)
def test_audited_patches_fail_closed_when_upstream_shape_changes(name):
    with pytest.raises(builder.MediaPlaybackBuildError, match="audited patch"):
        builder.patch_source(name, "/* New upstream implementation */")


def test_private_callback_patch_preserves_license_and_rejects_duplicate_declarations():
    license_text = "/* Original upstream copyright and permission notice */\n"
    declaration = "typedef void (*mp_stop_cb)(void *opaque);"
    result = builder.patch_source(
        "shared/media-playback/media-playback/media-playback.h", license_text + declaration
    )
    assert result.startswith(license_text)
    assert "void *opaque, bool error" in result
    with pytest.raises(builder.MediaPlaybackBuildError, match="audited patch"):
        builder.patch_source(
            "shared/media-playback/media-playback/media-playback.h", declaration * 2
        )


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
