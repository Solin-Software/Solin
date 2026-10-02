import re
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
    assert "Exec=solin %F" in desktop_file
    assert "Icon=com.solin.Solin" in desktop_file
    assert "<id>com.solin.Solin</id>" in metadata
    assert "<developer_name>Solin</developer_name>" in metadata
    assert "<developer " not in metadata
    assert "<mimetypes>" not in metadata
    assert "@VERSION@" in metadata
    assert "@RELEASE_DATE@" in metadata


def test_linux_workflow_packages_and_smokes_the_same_appimage_recipe():
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "scripts/package_solin_appimage.sh" in workflow
    assert "SOLIN_APPIMAGE_SKIP_BUILD: \"1\"" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in workflow
    assert "*.AppImage" in workflow
    assert "*.AppImage.sha256" in workflow


def test_windows_wrapper_delegates_to_wsl_and_propagates_failures():
    batch_wrapper = _read("scripts/build_solin_appimage.bat")
    powershell_wrapper = _read("scripts/build_solin_appimage.ps1")

    assert "build_solin_appimage.ps1" in batch_wrapper
    assert "exit /b %ERRORLEVEL%" in batch_wrapper
    assert '"--distribution", $Distribution' in powershell_wrapper
    assert '$Distribution = "Ubuntu-24.04"' in powershell_wrapper
    assert '"--cd", $ProjectRoot' in powershell_wrapper
    assert "SOLIN_APPIMAGE_SKIP_BUILD=1" in powershell_wrapper
    assert '"xvfb-run", "--auto-servernum"' in powershell_wrapper
    assert "$LASTEXITCODE -ne 0" in powershell_wrapper


def test_linux_ci_installs_all_pinned_obs_direct_dependency_providers():
    script = _read("scripts/build_solin.sh")
    packages = re.search(r"libobs_runtime_packages=\(\n(.*?)\n\)", script, re.DOTALL)
    assert packages is not None
    declared = packages[1].split()
    # The 47 external DT_NEEDED SONAMEs map to these 45 Noble packages.
    # libc6 supplies the loader, libc and libm; apt resolves transitive providers.
    required = {
        "libasound2t64", "libavcodec60", "libavdevice60", "libavformat60", "libavutil58",
        "libc6", "libcurl4t64", "libdrm2", "libegl1", "libfontconfig1", "libfreetype6",
        "libgcc-s1", "libglib2.0-0t64", "libjansson4", "libmbedcrypto7t64",
        "libmbedtls14t64", "libmbedx509-1t64", "libpci3", "libpipewire-0.3-0t64",
        "libpulse0", "librist4", "libspeexdsp1", "libsrt1.5-openssl", "libstdc++6",
        "libswresample4", "libswscale7", "libudev1", "libuuid1", "libv4l-0t64",
        "libva-drm2", "libva2", "libvpl2", "libwayland-client0", "libwayland-egl1",
        "libx11-6", "libx11-xcb1", "libx264-164", "libxcb-composite0", "libxcb-randr0",
        "libxcb-shm0", "libxcb-xfixes0", "libxcb-xinerama0", "libxcb1", "libxkbcommon0",
        "zlib1g",
    }
    assert set(declared) == required
    assert len(declared) == len(required)
    assert script.index("--print-native-packages") < script.index('PYTHON="$(resolve_python)"')
    for relative in (".github/workflows/build-solin-linux.yml", ".github/workflows/quality.yml"):
        workflow = _read(relative)
        assert "runs-on: ubuntu-24.04" in workflow
        assert "bash scripts/build_solin.sh --print-native-packages" in workflow
        assert '"${libobs_packages[@]}"' in workflow
        assert "add-apt-repository" not in workflow


def test_linux_mux_comes_from_the_checksum_pinned_official_release():
    script = _read("scripts/build_solin.sh")
    assert 'OBS_RUNTIME_VERSION="32.1.2"' in script
    assert 'OBS_RUNTIME_SHA256="a3bb1b0176604dad9e22710e057f0fdd76e8afb600e0e1914c30464ae49908e8"' in script
    assert 'OBS-Studio-${OBS_RUNTIME_VERSION}-Ubuntu-24.04-x86_64.deb' in script
    assert "https://github.com/obsproject/obs-studio/releases/download/" in script
    assert "sha256sum --check --status" in script
    assert script.index('"${OBS_RUNTIME_SHA256}" "${OBS_DOWNLOAD}"') < script.index(
        'mv -f -- "${OBS_DOWNLOAD}" "${OBS_RUNTIME_ARCHIVE}"'
    )
    assert 'dpkg-deb --extract "${OBS_RUNTIME_ARCHIVE}" "${OBS_EXTRACT_ROOT}"' in script
    assert "usr/local/bin/obs-ffmpeg-mux" in script
    assert 'trap \'rm -rf -- "${OBS_EXTRACT_ROOT}"\' EXIT' in script
    assert '--linux-mux-helper "${LINUX_MUX_HELPER}"' in script
    assert "dpkg --install" not in script and "dpkg-deb --install" not in script
    assert "apt-get install obs-studio" not in script


def test_linux_build_rejects_platforms_below_the_dependency_abi_floor():
    script = _read("scripts/build_solin.sh")
    assert 'platform.machine() != "x86_64"' in script
    assert 'tuple(map(int, version.split("."))) < (2, 38)' in script
    assert script.index("Ubuntu 22.04 is unsupported") < script.index('"${PYTHON}" -m nuitka')
