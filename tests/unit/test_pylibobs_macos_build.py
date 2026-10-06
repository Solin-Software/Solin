from __future__ import annotations

import hashlib
import io
import os
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


def test_macos_bundle_adapts_official_framework_and_plugin_layout(tmp_path):
    contents = _official_app(tmp_path / "official")
    runtime = tmp_path / "runtime"
    builder._copy_runtime(contents, runtime)

    framework_libobs = runtime / "Frameworks" / "libobs.framework" / "Versions" / "A" / "libobs"
    assert framework_libobs.read_bytes() == b"libobs"
    assert not (runtime / "libobs.dylib").exists()
    assert not (runtime / "Frameworks" / "QtCore.framework").exists()
    assert not (runtime / "Frameworks" / "OBS Helper.app").exists()
    assert (runtime / "obs-ffmpeg-mux").read_bytes() == b"mux"
    assert (runtime / "data" / "libobs" / "default.effect").read_bytes() == b"shader"
    for name in builder.REQUIRED_PLUGINS:
        assert (runtime / "obs-plugins" / f"{name}.dylib").read_bytes() == name.encode()
        assert (runtime / "data" / "obs-plugins" / name / "locale" / "en-US.ini").is_file()


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
    framework_libobs = _write(
        frameworks / "libobs.framework" / "Versions" / "A" / "libobs",
        b"\xcf\xfa\xed\xfe",
    )
    graphics = _write(frameworks / "libobs-opengl.dylib", b"\xcf\xfa\xed\xfe")
    _write(frameworks / "libavcodec.dylib", b"\xcf\xfa\xed\xfe")
    helper = _write(tmp_path / "obs-ffmpeg-mux", b"\xcf\xfa\xed\xfe")
    plugin = _write(tmp_path / "obs-plugins" / "image-source.dylib", b"\xcf\xfa\xed\xfe")
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
            if command[-1] == str(framework_libobs):
                result += (
                    "    @rpath/libobs.framework/Versions/A/libobs "
                    "(compatibility version 1.0.0)\n"
                )
                result += "    @rpath/libavcodec.dylib (compatibility version 61.0.0)\n"
            elif command[-1] == str(graphics):
                result += "    @rpath/libobs-opengl.dylib (compatibility version 1.0.0)\n"
                result += (
                    "    @rpath/libobs.framework/Versions/A/libobs "
                    "(compatibility version 1.0.0)\n"
                )
            else:
                result += (
                    "    @rpath/libobs.framework/Versions/A/libobs "
                    "(compatibility version 1.0.0)\n"
                )
                result += (
                    "    /old/OBS.app/Contents/Frameworks/libobs-opengl.dylib "
                    "(compatibility version 1.0.0)\n"
                )
            result += "    /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(builder, "_run", run)
    builder._relocate_runtime(tmp_path, "x86_64")

    assert [
        "install_name_tool",
        "-add_rpath",
        "@loader_path/../../..",
        str(framework_libobs),
    ] in calls
    assert [
        "install_name_tool",
        "-change",
        "@rpath/libavcodec.dylib",
        "@rpath/libavcodec.dylib",
        str(framework_libobs),
    ] not in calls
    for binary, relocated_libobs in (
        (graphics, "@loader_path/libobs.framework/Versions/A/libobs"),
        (helper, "@loader_path/Frameworks/libobs.framework/Versions/A/libobs"),
        (plugin, "@loader_path/../Frameworks/libobs.framework/Versions/A/libobs"),
    ):
        assert [
            "install_name_tool",
            "-change",
            "@rpath/libobs.framework/Versions/A/libobs",
            relocated_libobs,
            str(binary),
        ] in calls
    for binary in (helper, plugin):
        assert [
            "install_name_tool",
            "-change",
            "/old/OBS.app/Contents/Frameworks/libobs-opengl.dylib",
            "@rpath/libobs-opengl.dylib",
            str(binary),
        ] in calls
    assert ["install_name_tool", "-add_rpath", "@loader_path", str(graphics)] in calls
    assert ["install_name_tool", "-add_rpath", "@loader_path/Frameworks", str(helper)] in calls
    assert [
        "install_name_tool",
        "-add_rpath",
        "@loader_path/../Frameworks",
        str(plugin),
    ] in calls
    assert ["codesign", "--force", "--sign", "-", str(helper)] in calls
    assert ["codesign", "--force", "--sign", "-", str(framework_libobs)] in calls


@pytest.mark.parametrize(
    "architecture, dependency, error",
    [
        ("arm64", "", "does not support x86_64"),
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
    _write(
        tmp_path / "Frameworks" / "libobs.framework" / "Versions" / "A" / "libobs",
        b"\xcf\xfa\xed\xfe",
    )
    _write(tmp_path / "Frameworks" / "libobs-opengl.dylib", b"\xcf\xfa\xed\xfe")
    _write(tmp_path / "obs-ffmpeg-mux", b"\xcf\xfa\xed\xfe")
    _write(tmp_path / "obs-plugins" / "image-source.dylib", b"\xcf\xfa\xed\xfe")

    def run(command, **kwargs):
        result = architecture if command[0] == "lipo" else "header:\n"
        if command[0] == "otool" and "-L" in command and dependency:
            result += f"    {dependency} (compatibility version 1.0.0)\n"
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(builder, "_run", run)
    with pytest.raises(builder.BindingBuildError, match=error):
        builder._relocate_runtime(tmp_path, "x86_64")


def test_native_build_unmounts_even_when_runtime_layout_is_invalid(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(builder.BindingBuildError, match="one OBS application"):
        builder._extract_runtime(tmp_path / "obs.dmg", tmp_path / "runtime", "x86_64")
    assert calls[0][:2] == ["hdiutil", "attach"]
    assert calls[-1][:2] == ["hdiutil", "detach"]


def test_macos_locator_patch_prefers_framework_executable(tmp_path):
    locator = tmp_path / "pylibobs" / "_lib.py"
    locator.parent.mkdir(parents=True)
    locator.write_text(
        """from pathlib import Path

_LIBS_DIR = Path("_libs")

def probe(base, lib_name):
    # Try the most-likely path first per platform
    candidates = [
        base / lib_name,                          # win/linux flat layout
        base / "Frameworks" / lib_name,           # macOS .app layout
        base / "Frameworks" / "libobs.0.dylib",   # macOS versioned alias
    ]


def find_libobs():
    return "libobs"


# OBS helpers that libobs spawns

def get_obs_data_dir():
    libobs_path = Path(find_libobs())
    # Co-located bundled data (same directory as the DLL)
    for candidate in [
        libobs_path.parent / "data",
        libobs_path.parent.parent.parent / "data",  # OBS install layout
    ]:
        if candidate.exists():
            return str(candidate)


def get_obs_module_dirs():
    libobs_path = Path(find_libobs()).resolve()
    lib_dir = libobs_path.parent
    # 1. Bundled layout (fetch_libs.py extracts plugins next to obs.dll)
    bundled_plugins = lib_dir / "obs-plugins"
    bundled_data = lib_dir / "data" / "obs-plugins" / "%module%"
    if bundled_plugins.exists():
        return str(bundled_plugins), str(bundled_data)
""",
        encoding="utf-8",
    )

    builder._patch_macos_locator(tmp_path)

    patched = locator.read_text(encoding="utf-8")
    assert 'base / "Frameworks" / "libobs.framework" / "Versions" / "A" / "libobs"' in patched
    assert patched.index("libobs.framework") < patched.index("base / lib_name")
    assert "def _bundled_root" in patched
    assert 'candidates.append(bundled_root / "data")' in patched
    assert 'bundled_plugins = bundled_root / "obs-plugins"' in patched


def test_native_smoke_initializes_graphics_audio_modules_and_media_sources(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("LIBOBS_PATH", "/unrelated/system/libobs")
    monkeypatch.setenv("OBS_DATA_PATH", "/unrelated/system/shaders")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/unrelated/system/native")
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder._verify_source_runtime(tmp_path, "x86_64")
    command, options = calls[0]
    assert "LIBOBS_PATH" not in options["env"]
    assert "OBS_DATA_PATH" not in options["env"]
    assert "DYLD_LIBRARY_PATH" not in options["env"]
    assert options["env"]["PYTHONPATH"] == os.pathsep.join(
        (str(tmp_path), str(builder.REPO_ROOT / "src"))
    )
    assert "set_video_compat" in command[-1]
    assert "graphics_owner = set_video_compat" in command[-1]
    assert "\n_ = graphics_owner\n" in command[-1]
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
    monkeypatch.setattr(pylibobs, "__version__", "fixed-test-version")
    monkeypatch.setattr(builder, "_run", lambda command, **kwargs: exec(command[-1], {}))
    if missing is None:
        builder._verify_source_runtime(tmp_path, "x86_64")
        assert released == ["ffmpeg_source", "image_source", "color_source_v3"]
    else:
        with pytest.raises(AssertionError, match=f"Missing native video source: {missing}"):
            builder._verify_source_runtime(tmp_path, "x86_64")
        assert released[-1] == missing


@pytest.mark.parametrize(
    "system, architecture", [("win32", "AMD64"), ("linux", "x86_64")]
)
def test_non_macos_platforms_do_not_run_native_bootstrap(monkeypatch, system, architecture):
    monkeypatch.setattr(builder.sys, "platform", system)
    monkeypatch.setattr(builder.platform, "machine", lambda: architecture)
    monkeypatch.setattr(
        builder, "build_wheel", lambda *args: pytest.fail("unexpected native build")
    )
    assert builder.main([]) == 0


@pytest.mark.parametrize(
    ("host_architecture", "expected_architecture"),
    [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("arm64", "arm64"), ("aarch64", "arm64")],
)
def test_macos_builds_corrected_wheel_for_both_architectures(
    tmp_path, monkeypatch, host_architecture, expected_architecture
):
    calls = []
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: host_architecture)
    monkeypatch.setattr(
        builder,
        "build_wheel",
        lambda output, cache, architecture: calls.append((output, cache, architecture))
        or tmp_path / builder._wheel_name(architecture),
    )

    assert builder.main(["--output-dir", str(tmp_path / "wheels")]) == 0
    assert calls[0][2] == expected_architecture


def test_runtime_specs_match_official_obs_release_assets():
    assert builder._runtime_url(builder.RUNTIME_SPECS["x86_64"]).endswith(
        "OBS-Studio-32.1.2-macOS-Intel.dmg"
    )
    assert builder.RUNTIME_SPECS["x86_64"].sha256 == (
        "f7febee4c52e97930ffa9d8bcae79ee4c60c411827688cfbe36bc53edc51616e"
    )
    assert builder._runtime_url(builder.RUNTIME_SPECS["arm64"]).endswith(
        "OBS-Studio-32.1.2-macOS-Apple.dmg"
    )
    assert builder.RUNTIME_SPECS["arm64"].sha256 == (
        "2aeb3aaa99544fefd557f10ac6550e73df71540dd57528b2a1e6f39a55ebacfb"
    )
