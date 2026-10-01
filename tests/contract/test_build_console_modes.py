from __future__ import annotations

from pathlib import Path

from tests._paths import REPO_ROOT


def _read(relative_path: str) -> str:
    return (REPO_ROOT / Path(relative_path)).read_text(encoding="utf-8")


def test_windows_builds_attach_to_existing_terminal_without_spawning_one():
    for relative_path in (
        "scripts/build_solin.bat",
        ".github/workflows/build-solin-windows.yml",
    ):
        text = _read(relative_path)

        assert "--windows-console-mode=attach" in text
        assert "--windows-console-mode=disable" not in text
        assert "--windows-console-mode=force" not in text


def test_macos_bundle_keeps_finder_launch_gui_only():
    text = _read(".github/workflows/build-solin-macos.yml")

    assert "--macos-create-app-bundle" in text
    assert "--macos-app-console-mode=disable" in text
    assert "--macos-app-console-mode=force" not in text


def test_macos_build_packages_installed_sideview_dependency():
    workflow = _read(".github/workflows/build-solin-macos.yml")
    browser_tab = _read("src/solin/widgets/browser/tab.py")

    assert "--include-package=sideview" in workflow
    assert "src/native_webview_widget" not in workflow
    assert "from sideview import" in browser_tab
    assert '--include-data-files="${SIDEVIEW_DYLIB}=sideview/libsideview_native.dylib"' in workflow


def test_macos_build_persists_nuitka_and_c_compilation_cache():
    workflow = _read(".github/workflows/build-solin-macos.yml")

    assert "NUITKA_CACHE_DIR: ${{ github.workspace }}/.cache/nuitka" in workflow
    assert "path: .cache/nuitka" in workflow
    assert "${{ runner.os }}-${{ runner.arch }}-nuitka-" in workflow
    assert "hashFiles('requirements.txt', 'requirements-dev.txt')" in workflow
    assert "~/.cache/Nuitka" not in workflow


def test_linux_build_uses_shared_script_and_xcb_launcher():
    script = _read("scripts/build_solin.sh")
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "libsideview_native.so" in script
    assert "--include-package=sideview" in script
    assert "src/native_webview_widget" not in script
    assert "QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-xcb}" in script
    assert "WEBKIT_DISABLE_DMABUF_RENDERER" not in script
    assert 'rm -f "${DIST_DIR}/PySide6/qt-plugins/imageformats/libqtiff.so"' in script
    assert "xcb_helper_libraries=(" in script
    assert "xdotool_libraries=(" in script
    assert 'export PATH="$APP_DIR/bin${PATH:+:$PATH}"' in script
    assert '"${WORK_ROOT}/main.build"' in script
    assert "bash scripts/build_solin.sh" in workflow
    assert "gstreamer1.0-plugins-bad" in workflow
    assert "xdotool" in workflow
    assert "runs-on: ubuntu-22.04" in workflow
    assert "scripts/package_solin_appimage.sh" in workflow
    assert "*.AppImage.sha256" in workflow


def test_every_standalone_build_includes_and_qualifies_the_libobs_runtime():
    for relative in (
        "scripts/build_solin.bat", "scripts/build_solin.sh",
        ".github/workflows/build-solin-windows.yml", ".github/workflows/build-solin-macos.yml",
    ):
        recipe = _read(relative)
        assert "--include-package=pylibobs" in recipe
        assert "package_libobs_runtime.py" in recipe
        assert "--application-dir" in recipe and "--executable" in recipe
        assert "QtMultimedia" not in recipe


def test_macos_qualifies_final_library_layout_before_signing_and_disk_image():
    workflow = _read(".github/workflows/build-solin-macos.yml")
    qualify = workflow.index("- name: Stage and qualify packaged libobs runtime")
    assert workflow.index("- name: Normalize macOS Qt QML runtime links") < qualify
    assert workflow.index("- name: Verify compiled QML deployment") < qualify
    assert qualify < workflow.index("- name: Ad-hoc sign app bundle")
    assert qualify < workflow.index("- name: Create DMG")


def test_linux_ci_provides_a_graphics_session_for_libobs_build_qualification():
    assert 'xvfb-run --auto-servernum env SOLIN_PYTHON="$(command -v python)" bash scripts/build_solin.sh' in _read(".github/workflows/build-solin-linux.yml")
    assert "xvfb-run --auto-servernum python -m pytest" in _read(".github/workflows/build-solin-linux.yml")
    quality = _read(".github/workflows/quality.yml")
    assert "            xauth" in quality and "            xvfb" in quality
    assert "xvfb-run --auto-servernum python -m pytest tests/contract tests/integration tests/e2e" in quality
    assert "xvfb-run --auto-servernum python -m pytest tests/unit" in quality
