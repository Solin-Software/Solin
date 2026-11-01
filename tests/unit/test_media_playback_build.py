from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
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


@pytest.mark.parametrize(
    "platform, plugin_path, data_path, upstream_path",
    [
        (
            "windows",
            "core/solin-media-source/solin-media-source.dll",
            "core/solin-media-source/data",
            "core/obs-ffmpeg/obs-ffmpeg.dll",
        ),
        (
            "linux",
            "obs-modules/core/solin-media-source.so",
            "data/obs-modules/core/solin-media-source",
            "obs-modules/core/obs-ffmpeg.so",
        ),
        (
            "macos",
            "obs-plugins/solin-media-source.dylib",
            "data/obs-plugins/solin-media-source",
            "obs-plugins/obs-ffmpeg.dylib",
        ),
    ],
)
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("identical", [False, True])
def test_install_qualifies_candidate_before_publishing_module_and_receipt(
    tmp_path, monkeypatch, failed, identical, platform, plugin_path, data_path, upstream_path
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    binary = tmp_path / Path(plugin_path).name
    binary.write_bytes(b"new compiled module")
    installed = runtime / plugin_path
    installed.parent.mkdir(parents=True)
    previous = binary.read_bytes() if identical else b"previous module"
    installed.write_bytes(previous)
    unrelated = runtime / upstream_path
    unrelated.parent.mkdir(parents=True, exist_ok=True)
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
    relocated = []
    monkeypatch.setattr(builder, "_relocate_macos", lambda *args, **kwargs: relocated.append(True))
    replace = builder.os.replace

    def replace_unlocked(source, destination):
        if destination == installed and identical:
            pytest.fail("An identical loaded module must not be replaced")
        return replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", replace_unlocked)
    if failed:
        with pytest.raises(builder.MediaPlaybackBuildError, match="qualification"):
            builder.install_module(binary, sources, runtime, platform)
        assert installed.read_bytes() == previous
        assert not (runtime / builder.MANIFEST_NAME).exists()
    else:
        assert builder.install_module(binary, sources, runtime, platform) == installed
        receipt = json.loads((runtime / builder.MANIFEST_NAME).read_text())
        assert receipt["source_id"] == builder.SOURCE_ID
        assert receipt["source_sha256"] == builder.obs_sources.SOURCE_SHA256
        assert runtime / receipt["plugin"]["path"] == installed
        assert receipt["plugin"]["sha256"] == hashlib.sha256(installed.read_bytes()).hexdigest()
        assert (runtime / data_path / "COPYING").is_file()
        assert (runtime / data_path / "locale/en-US.ini").is_file()
    assert bool(relocated) == (platform == "macos")
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


def test_module_qualification_opens_normalized_paths_and_releases_the_source(tmp_path, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    opened, released = [], []
    binary, data = tmp_path / "module.dll", tmp_path / "data"
    source = SimpleNamespace(id=builder.SOURCE_ID, release=lambda: released.append(True))

    class Context:
        def __init__(self, **_options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def set_audio(self, **_options):
            pass

    library = SimpleNamespace(
        obs_open_module=lambda handle, binary, data: opened.append((binary, data)) or 0,
        obs_init_module=lambda _handle: True,
    )
    package = ModuleType("pylibobs")
    context_module = ModuleType("pylibobs.context")
    context_module.OBSContext = Context
    source_module = ModuleType("pylibobs.source")
    source_module.Source = SimpleNamespace(create=lambda *_args: source)
    ffi_module = ModuleType("pylibobs._ffi")
    ffi_module.ffi = SimpleNamespace(new=lambda _declaration: [object()])
    ffi_module.get_lib = lambda: library
    for module in (package, context_module, source_module, ffi_module):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "argv", ["-c", str(binary), str(data)])
    exec(compile(builder._VERIFY_MODULE, "<media-module-qualification>", "exec"), {})
    assert opened == [(binary.as_posix().encode(), data.as_posix().encode())]
    assert released


@pytest.mark.parametrize("provider_present", [True, False])
def test_linux_cmake_links_the_runtime_soname_and_rejects_only_legacy_provider(
    tmp_path, provider_present
):
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake is required for the native link contract")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    provider = runtime / "libobs.so.30"
    if provider_present:
        provider.write_bytes(b"current runtime provider")
    (runtime / "libobs.so.0").write_bytes(b"obsolete provider")
    recipe = (builder.SOURCE_DIRECTORY / "CMakeLists.txt").read_text(encoding="utf-8")
    # Configure the production resolver and Linux invocation with a controlled
    # provider directory. No compiler or OBS process is needed for this contract.
    function = re.search(r"(?ms)^function\(runtime_library\b.*?^endfunction\(\)", recipe)
    invocation = re.search(r"runtime_library\(obs libobs\.so(?:\.\d+)*\)", recipe)
    assert function is not None and invocation is not None
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.25)\n"
        "project(obs_link_contract LANGUAGES NONE)\n"
        "set(WIN32 FALSE)\n"
        f'set(SOLIN_OBS_RUNTIME "{runtime.as_posix()}")\n'
        "add_library(solin-media-runtime INTERFACE)\n"
        f"{function.group()}\n{invocation.group()}\n"
        "get_target_property(provider solin-media-runtime INTERFACE_LINK_LIBRARIES)\n"
        'file(WRITE "${CMAKE_BINARY_DIR}/obs-link.txt" "${provider}")\n',
        encoding="utf-8",
    )
    configured = subprocess.run(
        [cmake, "-S", str(tmp_path), "-B", str(tmp_path / "build")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if provider_present:
        assert configured.returncode == 0, configured.stdout + configured.stderr
        linked = (tmp_path / "build/obs-link.txt").read_text(encoding="utf-8")
        assert Path(linked).resolve() == provider.resolve()
    else:
        assert configured.returncode != 0
        assert str(provider.as_posix()) in configured.stderr
