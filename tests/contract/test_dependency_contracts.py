from __future__ import annotations

import platform
import re
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pylibobs
import pytest
import sideview
import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from tests._paths import REPO_ROOT

SIDEVIEW_REQUIREMENT = "sideview==0.4.1"


def _project_metadata() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_requirements() -> list[Requirement]:
    requirements: list[Requirement] = []
    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(("#", "-")):
            continue
        requirement = Requirement(candidate)
        requirements.append(requirement)
    return requirements


def test_locked_requirements_cover_every_direct_runtime_dependency() -> None:
    locked = _locked_requirements()

    for dependency in _project_metadata()["project"]["dependencies"]:
        expected = Requirement(dependency)
        assert expected in locked, f"{expected} is missing from requirements.txt"


@pytest.mark.parametrize("recipe", [
    "scripts/build_solin.bat", ".github/workflows/build-solin-windows.yml",
])
def test_windows_zoom_packaging_prepares_and_bundles_uia_interfaces(recipe):
    source = (REPO_ROOT / recipe).read_text(encoding="utf-8")
    preparation = source.index("prepare_zoom_bindings.py")
    assert preparation < source.index("-m nuitka", preparation)
    assert "--include-package=comtypes.gen" in source
    assert "--nofollow-import-to=comtypes.gen" not in source
    assert "--user-package-configuration-file=" in source
    assert "solin.nuitka-package.config.yml" in source
    assert "--verify-zoom-runtime" in source


def test_macos_build_includes_and_qualifies_the_binding_native_libraries():
    configuration_path = "packaging/macos/solin.nuitka-package.config.yml"
    configuration = yaml.safe_load((REPO_ROOT / configuration_path).read_text(encoding="utf-8"))
    entry, = configuration
    assert entry["module-name"] == "pylibobs"
    rule, = entry["dlls"]
    assert rule["when"] == "macos and standalone"
    assert rule["from_filenames"]["relative_path"] == "native"
    assert rule["from_filenames"]["suffixes"] == ["dylib"]
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/build-solin-macos.yml").read_text())
    commands = [step.get("run", "") for step in workflow["jobs"]["build"]["steps"]]
    compilation = next(command for command in commands if "-m nuitka" in command)
    assert f'--user-package-configuration-file="{configuration_path}"' in compilation
    verification = next(command for command in commands if "--verify-macos-runtime" in command)
    signature = next(command for command in commands if "codesign --verify" in command)
    dmg = next(command for command in commands if "\ncreate-dmg " in "\n" + command)
    assert commands.index(signature) < commands.index(verification) < commands.index(dmg)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM type libraries")
def test_bundled_com_bindings_accept_other_windows_timestamps_but_require_generator_version(
    tmp_path,
):
    from comtypes import _tlib_version_checker
    from comtypes.tools.codegenerator import version as generator_version

    configuration = yaml.safe_load(
        (REPO_ROOT / "packaging/windows/solin.nuitka-package.config.yml")
        .read_text(encoding="utf-8")
    )
    entry = next(
        item for item in configuration
        if item["module-name"] == "comtypes._tlib_version_checker"
    )
    rule, = entry["anti-bloat"]
    assert rule["when"] == "win32 and standalone"
    source = Path(_tlib_version_checker.__file__).read_text(encoding="utf-8")
    for original, replacement in rule["replacements_plain"].items():
        assert source.count(original) == 1, "Review the compiler integration after comtypes changes."
        source = source.replace(original, replacement)

    # A different Windows image or update changes the library timestamp without
    # changing its COM interface. Simulate it without touching any system file.
    typelib = tmp_path / "system.tlb"
    typelib.touch()
    namespace = {"typelib_path": str(typelib), "generator_version": generator_version}
    exec(compile(source, _tlib_version_checker.__file__, "exec"), namespace)
    exec("_check_version(generator_version, 0)", namespace)
    with pytest.raises(ImportError, match="Wrong version"):
        exec("_check_version('incompatible-generator', 0)", namespace)


def test_sideview_is_a_pinned_runtime_dependency():
    pyproject = _project_metadata()
    locked_requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert SIDEVIEW_REQUIREMENT in pyproject["project"]["dependencies"]
    assert SIDEVIEW_REQUIREMENT in locked_requirements


def test_sideview_is_not_vendored_in_the_solin_source_tree():
    pyproject = _project_metadata()
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert not (REPO_ROOT / "src" / "native_webview_widget").exists()
    assert "native_webview_widget" not in package_data


def test_installed_sideview_distribution_provides_the_native_backend():
    native_names = {
        "Windows": "sideview_native.dll",
        "Darwin": "libsideview_native.dylib",
        "Linux": "libsideview_native.so",
    }
    native_name = native_names.get(platform.system())
    if native_name is None:
        return

    package_dir = Path(sideview.__file__).resolve().parent

    assert sideview.__version__ == "0.4.1"
    assert version("sideview") == "0.4.1"
    assert (package_dir / native_name).is_file()


@pytest.mark.parametrize(
    ("system", "architecture"),
    [("darwin", "x86_64"), ("darwin", "arm64"), ("win32", "AMD64"), ("linux", "x86_64")],
)
def test_scene_engine_dependency_is_selected_on_every_supported_platform(system, architecture):
    environment = default_environment() | {"sys_platform": system, "platform_machine": architecture}
    selected = [
        requirement for requirement in _locked_requirements()
        if canonicalize_name(requirement.name) == "pylibobs"
        and (requirement.marker is None or requirement.marker.evaluate(environment))
    ]
    assert len(selected) == 1, f"Expected one binding dependency for {system}/{architecture}"
    requirement, = selected
    if requirement.url is not None:
        url = urlsplit(requirement.url)
        checksums = parse_qs(url.fragment).get("sha256", [])
        assert len(checksums) == 1 and re.fullmatch(r"[0-9a-f]{64}", checksums[0])
        name, _version, _build, tags = parse_wheel_filename(unquote(Path(url.path).name))
        assert canonicalize_name(name) == "pylibobs"
        if system == "win32":
            assert any(tag.platform == "win_amd64" for tag in tags)
        elif system == "linux":
            assert any(
                tag.platform.startswith("manylinux_") and tag.platform.endswith("_x86_64")
                for tag in tags
            )
        else:
            assert any(
                tag.platform.startswith("macosx_") and tag.platform.endswith(f"_{architecture}")
                for tag in tags
            )


def test_macos_installation_contains_the_native_media_backend():
    if platform.system() != "Darwin":
        return
    from pylibobs._ffi import ffi, get_lib
    from pylibobs._lib import find_libobs, get_bundled_modules

    architecture = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine().lower()
    ]
    root = Path(pylibobs.__file__).resolve().parent / "_libs" / "macos" / architecture
    framework_libobs = root / "Frameworks" / "libobs.framework" / "Versions" / "A" / "libobs"
    assert framework_libobs.is_file()
    assert Path(find_libobs()).resolve() == framework_libobs.resolve()
    assert (root / "Frameworks" / "libobs-opengl.dylib").is_file()
    assert (root / "data" / "libobs" / "default.effect").is_file()
    modules = {name: (Path(binary), Path(data)) for name, binary, data in get_bundled_modules()}
    for name in ("obs-ffmpeg", "image-source", "mac-capture", "mac-avcapture"):
        assert name in modules
        assert modules[name][0].is_file()
        assert modules[name][1].is_dir()
    from scripts.obs_sources import OBS_VERSION

    assert ffi.string(get_lib().obs_get_version_string()).decode("utf-8") == OBS_VERSION


def test_installed_pylibobs_provides_the_scene_engine_entry_points():
    """A pylibobs too old for this branch must fail here, not inside the sidecar.

    Exercise the capabilities used at the native boundary rather than duplicating
    the dependency version declared in the project manifests.
    """
    from pylibobs.display import (  # the projection route's per-source renderer
        render_main_texture_letterboxed,
        render_source_letterboxed,
    )

    assert callable(render_main_texture_letterboxed)
    assert callable(render_source_letterboxed)


def test_binding_exposes_the_native_task_and_rational_frame_rate_abi():
    """Reject an unpatched binding before native scheduling or camera discovery."""
    from pylibobs._ffi import ffi, get_lib

    assert ffi.typeof("obs_task_t").kind == "function"
    assert ffi.typeof("obs_task_handler_t").kind == "function"
    assert ffi.typeof("obs_scene_atomic_update_func").kind == "function"
    frame_getter = ffi.typeof(get_lib().obs_source_get_frame)
    assert frame_getter.args == (ffi.typeof("obs_source_t *"),)
    assert frame_getter.result == ffi.typeof("struct obs_source_frame *")
    assert ffi.sizeof("struct media_frames_per_second") == 8
