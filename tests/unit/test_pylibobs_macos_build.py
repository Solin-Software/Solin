from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_pylibobs_macos as builder


def _write(path: Path, content: bytes = b"test") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _official_app(root: Path) -> Path:
    contents = root / "OBS.app" / "Contents"
    framework = contents / "Frameworks" / "libobs.framework" / "Versions" / "A"
    _write(framework / "libobs", b"libobs")
    _write(framework / "Resources" / "default.effect", b"shader")
    _write(contents / "Frameworks" / "libobs-opengl.dylib", b"opengl")
    _write(contents / "Frameworks" / "libavcodec.dylib", b"codec")
    _write(contents / "Frameworks" / "QtCore.framework" / "QtCore", b"GUI")
    _write(contents / "Frameworks" / "OBS Helper.app" / "browser", b"browser")
    _write(contents / "MacOS" / "obs-ffmpeg-mux", b"mux")
    for name in builder.REQUIRED_PLUGINS:
        plugin = contents / "PlugIns" / f"{name}.plugin" / "Contents"
        _write(plugin / "MacOS" / name, name.encode())
        _write(plugin / "Resources" / "locale" / "en-US.ini", b"locale")
    return contents


def test_intel_bundle_adapts_official_framework_and_plugin_layout(tmp_path):
    contents = _official_app(tmp_path / "official")
    runtime = tmp_path / "runtime"
    builder._copy_runtime(contents, runtime)

    assert (runtime / "Frameworks" / "libobs.dylib").read_bytes() == b"libobs"
    assert not (runtime / "Frameworks" / "libobs.framework").exists()
    assert not (runtime / "Frameworks" / "QtCore.framework").exists()
    assert not (runtime / "Frameworks" / "OBS Helper.app").exists()
    assert (runtime / "obs-ffmpeg-mux").read_bytes() == b"mux"
    assert (runtime / "data" / "libobs" / "default.effect").read_bytes() == b"shader"
    for name in builder.REQUIRED_PLUGINS:
        # Both the package stager and upstream pylibobs lookup see the same
        # plugins/data; the latter looks relative to Frameworks/libobs.dylib.
        for base in (runtime, runtime / "Frameworks"):
            assert (base / "obs-plugins" / f"{name}.dylib").read_bytes() == name.encode()
            assert (base / "data" / "obs-plugins" / name / "locale" / "en-US.ini").is_file()


def test_missing_required_plugin_rejects_runtime(tmp_path):
    contents = _official_app(tmp_path / "official")
    (contents / "PlugIns" / "obs-ffmpeg.plugin" / "Contents" / "MacOS" / "obs-ffmpeg").unlink()
    with pytest.raises(builder.BindingBuildError, match="missing plugin obs-ffmpeg"):
        builder._copy_runtime(contents, tmp_path / "runtime")


def test_download_verifies_before_publishing_and_reuses_only_verified_cache(tmp_path, monkeypatch):
    content = b"pinned native artifact"
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(content)
    )
    destination = tmp_path / "cache" / "native.dmg"
    digest = hashlib.sha256(content).hexdigest()
    builder._download("https://example.invalid/native.dmg", destination, digest)
    assert destination.read_bytes() == content
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("cache miss")
    )
    builder._download("https://example.invalid/native.dmg", destination, digest)
    destination.write_bytes(b"tampered")
    with pytest.raises(builder.BindingBuildError, match="Cached artifact failed SHA-256"):
        builder._download("https://example.invalid/native.dmg", destination, digest)


def test_download_checksum_failure_leaves_no_usable_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        builder.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong")
    )
    with pytest.raises(builder.BindingBuildError, match="Downloaded artifact failed SHA-256"):
        builder._download("https://example.invalid/native.dmg", tmp_path / "native.dmg", "0" * 64)
    assert not list(tmp_path.iterdir())


def test_relocation_uses_one_libobs_identity_and_rebases_host_dependent_paths(
    tmp_path, monkeypatch
):
    frameworks = tmp_path / "Frameworks"
    core = _write(frameworks / "libobs.dylib", b"\xcf\xfa\xed\xfe")
    graphics = _write(frameworks / "libobs-opengl.dylib", b"\xcf\xfa\xed\xfe")
    helper = _write(tmp_path / "obs-ffmpeg-mux", b"\xcf\xfa\xed\xfe")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        result = ""
        if command[0] == "lipo":
            result = "x86_64 arm64\n"
        elif command[0] == "otool" and "-l" in command:
            result = "cmd LC_RPATH\n    path @executable_path/../Frameworks (offset 12)\n"
        elif command[0] == "otool" and "-L" in command:
            result = command[-1] + ":\n"
            if command[-1] != str(core):
                result += (
                    "    @rpath/libobs.framework/Versions/A/libobs (compatibility version 1.0.0)\n"
                )
                result += "    /old/OBS.app/Contents/Frameworks/libobs-opengl.dylib (compatibility version 1.0.0)\n"
            result += "    /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(builder, "_run", run)
    builder._relocate_runtime(tmp_path)

    assert ["install_name_tool", "-id", "@rpath/libobs.dylib", str(core)] in calls
    for binary in (graphics, helper):
        assert [
            "install_name_tool",
            "-change",
            "@rpath/libobs.framework/Versions/A/libobs",
            "@rpath/libobs.dylib",
            str(binary),
        ] in calls
        assert [
            "install_name_tool",
            "-change",
            "/old/OBS.app/Contents/Frameworks/libobs-opengl.dylib",
            "@rpath/libobs-opengl.dylib",
            str(binary),
        ] in calls
    assert ["install_name_tool", "-add_rpath", "@loader_path/Frameworks", str(helper)] in calls
    assert ["codesign", "--force", "--sign", "-", str(helper)] in calls


@pytest.mark.parametrize(
    "architecture, dependency, error",
    [
        ("arm64", "", "does not support Intel"),
        ("x86_64", "@rpath/not-bundled.dylib", "Unbundled native dependency"),
    ],
)
def test_relocation_rejects_wrong_architecture_and_missing_native_dependency(
    tmp_path,
    monkeypatch,
    architecture,
    dependency,
    error,
):
    _write(tmp_path / "Frameworks" / "libobs.dylib", b"\xcf\xfa\xed\xfe")

    def run(command, **kwargs):
        result = architecture if command[0] == "lipo" else "header:\n"
        if command[0] == "otool" and "-L" in command and dependency:
            result += f"    {dependency} (compatibility version 1.0.0)\n"
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(builder, "_run", run)
    with pytest.raises(builder.BindingBuildError, match=error):
        builder._relocate_runtime(tmp_path)


def test_native_build_unmounts_even_when_runtime_layout_is_invalid(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(builder.BindingBuildError, match="one OBS application"):
        builder._extract_runtime(tmp_path / "obs.dmg", tmp_path / "runtime")
    assert calls[0][:2] == ["hdiutil", "attach"]
    assert calls[-1][:2] == ["hdiutil", "detach"]


def test_native_smoke_initializes_graphics_audio_modules_and_media_sources(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("LIBOBS_PATH", "/unrelated/system/libobs")
    monkeypatch.setenv("OBS_DATA_PATH", "/unrelated/system/shaders")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/unrelated/system/native")
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder._verify_source_runtime(tmp_path)
    command, options = calls[0]
    assert "LIBOBS_PATH" not in options["env"]
    assert "OBS_DATA_PATH" not in options["env"]
    assert "DYLD_LIBRARY_PATH" not in options["env"]
    assert options["env"]["PYTHONPATH"] == str(tmp_path)
    assert "context.set_video" in command[-1]
    assert "context.set_audio" in command[-1]
    assert "context.load_modules" in command[-1]
    assert "Source.create" in command[-1]
    assert "source.release()" in command[-1]
    compile(command[-1], "<native smoke>", "exec")


@pytest.mark.parametrize("missing", [None, "ffmpeg_source", "image_source", "color_source_v3"])
def test_native_smoke_rejects_placeholder_sources_and_always_releases_them(
    tmp_path, monkeypatch, missing,
):
    import pylibobs
    import pylibobs._ffi as binding
    import pylibobs._lib as libraries

    released = []

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def set_video(self, *args, **kwargs):
            pass

        def set_audio(self):
            pass

        def load_modules(self):
            pass

    def create(kind, *args):
        return SimpleNamespace(_ptr=kind, release=lambda: released.append(kind))

    monkeypatch.setattr(pylibobs, "OBSContext", Context)
    monkeypatch.setattr(pylibobs.Source, "create", create)
    monkeypatch.setattr(binding, "ffi", SimpleNamespace(string=lambda value: value))
    monkeypatch.setattr(binding, "get_lib", lambda: SimpleNamespace(
        obs_get_version_string=lambda: builder.OBS_VERSION.encode(),
        obs_source_get_output_flags=lambda kind: 0 if kind == missing else 1,
    ))
    monkeypatch.setattr(libraries, "get_bundled_modules", lambda: [
        (name, "binary", "data") for name in builder.REQUIRED_PLUGINS
    ])
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: exec(command[-1], {}))
    if missing is None:
        builder._verify_source_runtime(tmp_path)
        assert released == ["ffmpeg_source", "image_source", "color_source_v3"]
    else:
        with pytest.raises(AssertionError, match=f"Missing native video source: {missing}"):
            builder._verify_source_runtime(tmp_path)
        assert released[-1] == missing


@pytest.mark.parametrize(
    "system, architecture", [("win32", "AMD64"), ("linux", "x86_64"), ("darwin", "arm64")]
)
def test_published_wheel_platforms_do_not_run_intel_bootstrap(monkeypatch, system, architecture):
    monkeypatch.setattr(builder.sys, "platform", system)
    monkeypatch.setattr(builder.platform, "machine", lambda: architecture)
    monkeypatch.setattr(
        builder, "build_wheel", lambda *args: pytest.fail("unexpected native build")
    )
    assert builder.main([]) == 0
