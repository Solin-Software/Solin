from __future__ import annotations

import platform
import tomllib
from importlib.metadata import version
from pathlib import Path

import pylibobs
import sideview
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests._paths import REPO_ROOT

SIDEVIEW_REQUIREMENT = "sideview==0.4.1"
# The release carrying every entry point the libobs engine calls, plus the NV12
# chroma-plane over-read fix the Windows virtual camera trips over.
PYLIBOBS_VERSION = Version("0.1.2")


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

    Compared as a parsed requirement rather than a literal: the two manifests
    quote the environment marker differently.
    """
    pinned = _locked_requirements().get(canonicalize_name("pylibobs"))

    assert pinned is not None, "pylibobs is missing from requirements.txt"
    assert str(pinned.specifier) == f"=={PYLIBOBS_VERSION}"


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
