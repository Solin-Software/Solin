from __future__ import annotations

import platform
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pylibobs
import pytest
import sideview
import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests._paths import REPO_ROOT

SIDEVIEW_REQUIREMENT = "sideview==0.4.1"
# The release carrying every entry point the libobs engine calls, plus the NV12
# chroma-plane over-read fix the Windows virtual camera trips over.
PYLIBOBS_VERSION = Version("32.2.2")


def _project_metadata() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_requirements() -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(("#", "-")):
            continue
        requirement = Requirement(candidate)
        requirements[canonicalize_name(requirement.name)] = requirement
    return requirements


def test_locked_requirements_cover_every_direct_runtime_dependency() -> None:
    locked = _locked_requirements()

    for dependency in _project_metadata()["project"]["dependencies"]:
        expected = Requirement(dependency)
        actual = locked.get(canonicalize_name(expected.name))

        assert actual is not None, f"{expected.name} is missing from requirements.txt"
        assert actual.specifier == expected.specifier
        assert str(actual.marker) == str(expected.marker)


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


def test_pylibobs_is_a_pinned_runtime_dependency():
    """libobs is the only media engine on this branch, so pin its binding.

    Keep the manifests and the wheel bootstrap on the same binding release.
    """
    pinned = _locked_requirements().get(canonicalize_name("pylibobs"))

    assert pinned is not None, "pylibobs is missing from requirements.txt"
    assert str(pinned.specifier) == f"=={PYLIBOBS_VERSION}"
    from scripts.build_pylibobs import PYLIBOBS_VERSION as bootstrap_binding_version

    assert Version(bootstrap_binding_version) == PYLIBOBS_VERSION


@pytest.mark.parametrize(
    ("system", "architecture"),
    [("darwin", "x86_64"), ("darwin", "arm64"), ("win32", "AMD64"), ("linux", "x86_64")],
)
def test_scene_engine_dependency_is_selected_on_every_supported_platform(system, architecture):
    requirement = _locked_requirements()[canonicalize_name("pylibobs")]
    environment = default_environment() | {"sys_platform": system, "platform_machine": architecture}
    assert requirement.marker is None or requirement.marker.evaluate(environment)


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
    assert ffi.string(get_lib().obs_get_version_string()).decode("utf-8") == "32.2.2"


def test_installed_pylibobs_provides_the_scene_engine_entry_points():
    """A pylibobs too old for this branch must fail here, not inside the sidecar.

    An older build has no projection renderer and over-reads NV12 chroma planes in
    the raw video callback; the second takes the sidecar down with no Python
    traceback, which is expensive to diagnose from a user's log.

    Checked against ``pylibobs.__version__`` rather than the installed distribution
    metadata: an editable dev checkout keeps whatever version it was installed with,
    so the metadata goes stale while the code is current.
    """
    assert Version(pylibobs.__version__) >= PYLIBOBS_VERSION

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
