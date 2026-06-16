from __future__ import annotations

import ast
import platform

from native_webview_widget.abi import EVENTS, REQUIRED_EXPORTS
from scripts import validate_native_webview
from tests._paths import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "native_webview_widget"


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
    }


def test_native_webview_binaries_have_expected_container_headers():
    assert validate_native_webview.validate_python_package(PACKAGE_DIR) == []
    assert validate_native_webview.validate_binary_files(PACKAGE_DIR) == []


def test_native_webview_current_platform_exports_match_abi_manifest():
    if platform.system() not in {"Windows", "Darwin"}:
        return

    assert validate_native_webview.validate_current_platform_exports(PACKAGE_DIR) == []
