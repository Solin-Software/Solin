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


def test_linux_build_uses_shared_script_and_xcb_launcher():
    script = _read("scripts/build_solin.sh")
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "libnative_webview_widget.so" in script
    assert "scripts/validate_native_webview.py --require-linux" in script
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
