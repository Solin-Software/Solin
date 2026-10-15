from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile

import pytest

from scripts import build_media_extract as builder
from scripts import package_libobs_runtime as packaging


def _sdk_zip(*, escaping=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for component in builder.COMPONENTS:
            header = "swscale.h" if component == "swscale" else f"{component}.h"
            archive.writestr(f"include/lib{component}/{header}", "public headers")
            archive.writestr(f"lib/{component}.lib", "import library")
        archive.writestr("bin/avcodec-61.dll", "SDK runtime must stay in the SDK")
        if escaping:
            archive.writestr("../escaped", "invalid")
    return output.getvalue()


def test_sdk_checksum_is_required_before_extracting_or_reusing_cache(tmp_path, monkeypatch):
    payload = _sdk_zip()
    spec = builder.SdkSpec("sdk.zip", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(payload)
    )
    archive = builder._download_sdk(spec, tmp_path)
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("cache miss")
    )
    assert builder._download_sdk(spec, tmp_path) == archive
    archive.write_bytes(b"corrupted")
    with pytest.raises(builder.MediaExtractBuildError, match="SHA-256"):
        builder._download_sdk(spec, tmp_path)


def test_bad_download_never_publishes_a_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"wrong")
    )
    with pytest.raises(builder.MediaExtractBuildError, match="SHA-256"):
        builder._download_sdk(builder.SdkSpec("sdk.zip", "0" * 64), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("escaping", [False, True])
def test_sdk_extraction_confines_files_to_verified_cache(tmp_path, monkeypatch, escaping):
    payload = _sdk_zip(escaping=escaping)
    archive = tmp_path / "sdk.zip"
    archive.write_bytes(payload)
    monkeypatch.setitem(
        builder.SDK_SPECS,
        ("windows", "x86_64"),
        builder.SdkSpec("sdk.zip", hashlib.sha256(payload).hexdigest()),
    )
    if escaping:
        with pytest.raises(builder.MediaExtractBuildError, match="escaping"):
            builder.prepare_sdk("windows", "x86_64", tmp_path)
        assert not (tmp_path / "escaped").exists()
    else:
        sdk = builder.prepare_sdk("windows", "x86_64", tmp_path)
        assert (sdk / "include/libavcodec/avcodec.h").is_file()
        assert (sdk / "lib/avcodec.lib").is_file()


@pytest.mark.parametrize("failure", [False, True])
def test_install_qualifies_before_replacing_only_the_owned_helper(tmp_path, monkeypatch, failure):
    executable, runtime = tmp_path / "build/solin-media-extract.exe", tmp_path / "runtime"
    executable.parent.mkdir()
    executable.write_bytes(b"new helper")
    runtime.mkdir()
    destination = runtime / "solin-media-extract.exe"
    destination.write_bytes(b"previous helper")
    (runtime / "avcodec-61.dll").write_bytes(b"existing runtime")
    monkeypatch.setattr(packaging, "stage_media_extract_dependencies", lambda *_args: None)
    monkeypatch.setattr(packaging, "relocate_media_extract", lambda *_args: None)

    def qualify(candidate, **_kwargs):
        assert candidate.parent == runtime
        assert candidate.read_bytes() == b"new helper"
        assert destination.read_bytes() == b"previous helper"
        if failure:
            raise packaging.LibobsPackagingError("qualification failed")

    monkeypatch.setattr(packaging, "verify_media_extract", qualify)
    if failure:
        with pytest.raises(packaging.LibobsPackagingError, match="qualification"):
            builder.install_helper(executable, runtime, "windows", "x86_64")
        assert destination.read_bytes() == b"previous helper"
    else:
        assert builder.install_helper(executable, runtime, "windows", "x86_64") == destination
        assert destination.read_bytes() == b"new helper"
    assert (runtime / "avcodec-61.dll").read_bytes() == b"existing runtime"
    assert not list(runtime.glob(".solin-media-extract-*"))


def test_linux_headers_come_from_matching_pkg_config_without_an_sdk_download(tmp_path, monkeypatch):
    include = tmp_path / "usr/include/x86_64-linux-gnu"
    for component in builder.COMPONENTS:
        path = include / f"lib{component}/version.h"
        path.parent.mkdir(parents=True)
        path.write_text("distribution headers")
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, str(include), ""),
    )
    monkeypatch.setattr(
        builder.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("Linux must use apt"),
    )
    sdk = builder.prepare_sdk("linux", "x86_64", tmp_path / "cache")
    assert (sdk / "include/libavformat/version.h").read_text() == "distribution headers"


def test_linux_cmake_build_always_supplies_the_distribution_header_prefix(tmp_path, monkeypatch):
    build = tmp_path / "native"
    build.mkdir()
    executable = build / "solin-media-extract"
    executable.write_bytes(b"built helper")
    runtime, sdk = tmp_path / "runtime", tmp_path / "SDK"
    commands = []
    monkeypatch.setattr(builder, "target", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(builder, "installed_runtime", lambda *_args: runtime)
    monkeypatch.setattr(builder, "prepare_sdk", lambda *_args: sdk)
    monkeypatch.setattr(builder.shutil, "which", lambda _name: "/usr/bin/cmake")
    monkeypatch.setattr(builder, "_run", commands.append)
    monkeypatch.setattr(builder, "install_helper", lambda built, *_args: built)
    assert (
        builder.build_media_extract(
            build_directory=build,
            cache_directory=tmp_path / "cache",
            configuration="Release",
            jobs=2,
        )
        == executable
    )
    assert f"-DSOLIN_FFMPEG_ROOT={sdk}" in commands[0]
    assert "-DCMAKE_BUILD_TYPE=Release" in commands[0]
    assert commands[1][-2:] == ["--parallel", "2"]
    assert commands[2][0].replace("\\", "/") == "/usr/bin/ctest"


@pytest.mark.parametrize(
    "mutation",
    [None, "major", "older_runtime", "foreign_library", "missing_library", "boolean_version"],
)
def test_qualification_requires_matching_versions_and_actual_private_library_origins(
    tmp_path, mutation
):
    root = tmp_path / "runtime"
    root.mkdir()
    abi = {}
    for name, major in packaging._MEDIA_EXTRACT_ABI["windows"].items():
        library = root / f"{name}-{major}.dll"
        library.write_bytes(b"runtime")
        abi[name] = {
            "compiled": major << 16,
            "runtime": major << 16,
            "library": str(library.resolve()),
        }
    entry = abi["avcodec"]
    if mutation == "major":
        entry["runtime"] += 1 << 16
    elif mutation == "older_runtime":
        entry["runtime"] -= 1
    elif mutation == "foreign_library":
        foreign = tmp_path / "SDK/avcodec-61.dll"
        foreign.parent.mkdir()
        foreign.write_bytes(b"foreign runtime")
        entry["library"] = str(foreign.resolve())
    elif mutation == "missing_library":
        entry.pop("library")
    elif mutation == "boolean_version":
        entry["runtime"] = True
    if mutation is None:
        packaging._verify_media_extract_abi({"abi": abi}, root, "windows")
    else:
        with pytest.raises(packaging.LibobsPackagingError):
            packaging._verify_media_extract_abi({"abi": abi}, root, "windows")


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON\n",
        b'{"protocol":2,"status":"ok"}\n',
        b'{"protocol":1,"status":"error"}\n',
        b"[]\n",
    ],
)
def test_qualification_rejects_invalid_or_failed_protocol(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(
        packaging.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, payload, b""),
    )
    with pytest.raises(packaging.LibobsPackagingError):
        packaging._run_media_extract(["helper"], environment={}, cwd=tmp_path)


def test_real_helper_qualification_disables_external_cli_and_graphics_lookup(tmp_path, monkeypatch):
    helper = tmp_path / "runtime/solin-media-extract.exe"
    helper.parent.mkdir()
    helper.write_bytes(b"native helper")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for filename in ("video.mp4", "video-cover.mp4", "audio-cover.mp3"):
        (fixtures / filename).write_bytes(b"fixture")
    abi = {}
    for name, major in packaging._MEDIA_EXTRACT_ABI["windows"].items():
        path = helper.parent / f"{name}-{major}.dll"
        path.write_bytes(b"runtime")
        abi[name] = {
            "compiled": major << 16,
            "runtime": major << 16,
            "library": str(path.resolve()),
        }
    for name in (
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "LIBOBS_PATH",
        "SOLIN_MEDIA_EXTRACT_FFMPEG_DIR",
    ):
        monkeypatch.setenv(name, "injected")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] != helper.parent
        assert kwargs["timeout"] == 35
        assert kwargs["env"].get("DISPLAY") is None
        assert kwargs["env"].get("LD_LIBRARY_PATH") is None
        assert kwargs["env"].get("SOLIN_MEDIA_EXTRACT_FFMPEG_DIR") is None
        assert "ffmpeg" not in kwargs["env"]["PATH"].casefold()
        header = {"protocol": 1, "status": "ok"}
        pixels = b""
        if "--version" in command:
            header["abi"] = abi
        else:
            policy = command[command.index("--image-policy") + 1]
            source = command[command.index("--source") + 1]
            header.update(
                title="Solin — ação 🎵" if source.endswith(".wav") else "Canção — ação 🎵",
                duration_ms=2000,
                has_video=source.endswith(".mp4"),
            )
            if policy != "none":
                header["image"] = {
                    "width": 2,
                    "height": 2,
                    "stride": 8,
                    "kind": policy,
                    "bytes": 16,
                }
                pixel = (
                    b"\xff" * 4
                    if source.endswith(".png")
                    else (b"\x00\x00\xff\xff" if policy == "frame" else b"\x00\xff\x00\xff")
                )
                pixels = pixel * 4
        return subprocess.CompletedProcess(
            command, 0, json.dumps(header).encode() + b"\n" + pixels, b""
        )

    monkeypatch.setattr(packaging.subprocess, "run", run)
    receipt = packaging.verify_media_extract(
        helper, target_platform="windows", qualification_fixtures=fixtures
    )
    assert receipt["helper"]["sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    assert all(
        not record["path"].startswith(str(tmp_path)) for record in receipt["libraries"].values()
    )
    assert set(receipt["libraries"]) == {"avformat", "avcodec", "avutil", "swscale"}
    assert len(commands) == 6
