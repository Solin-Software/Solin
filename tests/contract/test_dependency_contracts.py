from __future__ import annotations

import platform
import tomllib
from importlib.metadata import version
from pathlib import Path

import sideview
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests._paths import REPO_ROOT

SIDEVIEW_REQUIREMENT = "sideview==0.4.1"


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
