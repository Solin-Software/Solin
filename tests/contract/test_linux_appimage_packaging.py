from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_linux_launcher_keeps_dmabuf_renderer_enabled_by_default():
    build_script = _read("scripts/build_solin.sh")
    app_run = _read("packaging/linux/appimage/AppRun")

    assert "WEBKIT_DISABLE_DMABUF_RENDERER" not in build_script
    assert "WEBKIT_DISABLE_DMABUF_RENDERER" not in app_run
    assert "QT_QPA_PLATFORM" in build_script


def test_linux_build_bundles_non_core_xcb_helpers():
    build_script = _read("scripts/build_solin.sh")

    for library_name in (
        "libxkbcommon-x11.so.0",
        "libxcb-cursor.so.0",
        "libxcb-icccm.so.4",
        "libxcb-util.so.1",
        "libxcb-image.so.0",
        "libxcb-keysyms.so.1",
        "libxcb-randr.so.0",
        "libxcb-render-util.so.0",
        "libxcb-xfixes.so.0",
        "libxcb-shape.so.0",
        "libxcb-xkb.so.1",
    ):
        assert library_name in build_script

    assert "libX11.so.6" not in build_script
    assert "libGL.so.1" not in build_script


def test_linux_build_bundles_xdotool_and_its_redistribution_notices():
    build_script = _read("scripts/build_solin.sh")
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "xdotool_libraries=(" in build_script
    for library_name in ("libxdo.so.3", "libXtst.so.6", "libXinerama.so.1"):
        assert library_name in build_script

    assert 'patchelf --set-rpath "\\$ORIGIN/.."' in build_script
    assert 'export PATH="$APP_DIR/bin${PATH:+:$PATH}"' in build_script
    assert "/usr/share/doc/xdotool/copyright" in build_script
    assert "/usr/share/doc/libxtst6/copyright" in build_script
    assert "/usr/share/doc/libxinerama1/copyright" in build_script
    assert "xdotool" in workflow


def test_appimage_recipe_has_required_appdir_metadata_and_pinned_tools():
    package_script = _read("scripts/package_solin_appimage.sh")
    launcher = _read("packaging/linux/appimage/solin")
    desktop_file = _read("packaging/linux/appimage/com.solin.Solin.desktop")
    metadata = _read("packaging/linux/appimage/com.solin.Solin.appdata.xml")

    assert 'APPIMAGETOOL_VERSION="1.9.1"' in package_script
    assert 'APPIMAGETOOL_SHA256="' in package_script
    assert 'TYPE2_RUNTIME_SHA256="' in package_script
    assert "sha256sum --check --status" in package_script
    assert "SOLIN_APPIMAGE_SKIP_BUILD" in package_script
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in package_script
    assert 'exec "$APPDIR/usr/lib/solin/run-solin"' in launcher
    assert "Exec=solin %U" in desktop_file
    assert "Icon=com.solin.Solin" in desktop_file
    assert "<id>com.solin.Solin</id>" in metadata
    assert "@VERSION@" in metadata
    assert "@RELEASE_DATE@" in metadata


def test_linux_workflow_packages_and_smokes_the_same_appimage_recipe():
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "scripts/package_solin_appimage.sh" in workflow
    assert "SOLIN_APPIMAGE_SKIP_BUILD: \"1\"" in workflow
    assert "runs-on: ubuntu-22.04" in workflow
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in workflow
    assert "*.AppImage" in workflow
    assert "*.AppImage.sha256" in workflow


def test_windows_wrapper_delegates_to_wsl_and_propagates_failures():
    batch_wrapper = _read("scripts/build_solin_appimage.bat")
    powershell_wrapper = _read("scripts/build_solin_appimage.ps1")

    assert "build_solin_appimage.ps1" in batch_wrapper
    assert "exit /b %ERRORLEVEL%" in batch_wrapper
    assert '"--distribution", $Distribution' in powershell_wrapper
    assert '"--cd", $ProjectRoot' in powershell_wrapper
    assert "SOLIN_APPIMAGE_SKIP_BUILD=1" in powershell_wrapper
    assert "$LASTEXITCODE -ne 0" in powershell_wrapper
