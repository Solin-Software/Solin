from __future__ import annotations

import ast
import platform
import tomllib

import pytest

from native_webview_widget.abi import (
    EVENTS,
    NATIVE_VIEW_EXPORT,
    REQUIRED_EXPORTS,
    ZOOM_EXPORTS,
    native_library_candidates,
    required_exports_for_system,
)
from scripts import validate_native_webview
from tests._paths import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "src" / "native_webview_widget"


def _backend_configured_symbols() -> set[str]:
    tree = ast.parse(
        (PACKAGE_DIR / "_backend.py").read_text(encoding="utf-8"),
        filename=str(PACKAGE_DIR / "_backend.py"),
    )
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            if (
                isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"
                and node.value.attr == "_lib"
                and node.attr.startswith("nwv_")
            ):
                symbols.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("nwv_"):
                symbols.add(node.value)
    return symbols


def test_native_webview_abi_manifest_matches_backend_usage():
    assert set(REQUIRED_EXPORTS) == _backend_configured_symbols()


def test_native_webview_package_uses_src_layout_and_includes_native_binaries():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert PACKAGE_DIR.is_dir()
    assert not (REPO_ROOT / "native_webview_widget" / "__init__.py").exists()
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert pyproject["tool"]["setuptools"]["package-data"]["native_webview_widget"] == [
        "*.dll",
        "*.dylib",
        "libnative_webview_widget.so",
    ]


def test_native_webview_event_ids_are_stable():
    assert EVENTS == {
        "ready": 1,
        "navigation_started": 2,
        "navigation_finished": 3,
        "navigation_failed": 4,
        "title_changed": 5,
        "download_requested": 6,
        "new_window_requested": 7,
        "script_message": 8,
        "zoom_factor_changed": 9,
    }


def test_zoom_exports_are_required_for_windows_and_optional_for_vendored_macos():
    assert set(ZOOM_EXPORTS) <= set(required_exports_for_system("Windows"))
    assert set(ZOOM_EXPORTS).isdisjoint(required_exports_for_system("Darwin"))


def test_native_view_export_is_required_only_for_linux_embedding():
    assert NATIVE_VIEW_EXPORT in required_exports_for_system("Linux")
    assert NATIVE_VIEW_EXPORT not in required_exports_for_system("Windows")
    assert NATIVE_VIEW_EXPORT not in required_exports_for_system("Darwin")


def test_native_webview_binaries_have_expected_container_headers():
    assert validate_native_webview.validate_python_package(PACKAGE_DIR) == []
    assert validate_native_webview.validate_binary_files(PACKAGE_DIR) == []


def test_native_webview_validator_can_require_linux_artifact(tmp_path):
    errors = validate_native_webview.validate_binary_files(tmp_path, require_linux=True)

    assert any("Missing Linux native webview binary" in error for error in errors)


def test_native_webview_current_platform_exports_match_abi_manifest():
    system = platform.system()
    if system not in {"Windows", "Darwin", "Linux"}:
        return
    if system == "Linux" and not any(
        path.is_file() for path in native_library_candidates(system, PACKAGE_DIR)
    ):
        pytest.skip("Linux native artifact is supplied separately from the source tree.")

    assert validate_native_webview.validate_current_platform_exports(PACKAGE_DIR) == []
