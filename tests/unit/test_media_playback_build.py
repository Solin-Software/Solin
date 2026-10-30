from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from scripts import build_media_playback as builder


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
        assert receipt["source_sha256"] == builder.obs_sources.SOURCE_SHA256
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
