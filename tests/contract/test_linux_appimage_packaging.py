import re
from pathlib import Path
import sys

import pytest
import yaml


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
    assert 'TYPE2_RUNTIME_VERSION="20251108"' in package_script
    assert (
        'TYPE2_RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"'
        in package_script
    )
    assert (
        "type2-runtime/releases/download/${TYPE2_RUNTIME_VERSION}/runtime-x86_64"
        in package_script
    )
    assert "type2-runtime/releases/download/continuous" not in package_script
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
    configuration = yaml.load(workflow, Loader=yaml.BaseLoader)
    assert configuration["jobs"]["build"]["runs-on"] == "ubuntu-26.04"
    assert 'APPIMAGE_EXTRACT_AND_RUN: "1"' in workflow
    assert "test_packaged_app_smoke.py::test_packaged_app_survives_startup_window" in workflow
    assert "test_packaged_app_smoke.py::test_packaged_http_runtime" in workflow
    assert "test_packaged_app_smoke.py::test_packaged_media_runtime" in workflow
    assert "timeout --signal=TERM" not in workflow
    assert "-linux-x86_64.AppImage" in workflow
    assert "dist/Solin-${{ env.APP_VERSION }}-linux-x86_64.AppImage" in workflow


def test_windows_wrapper_delegates_to_wsl_and_propagates_failures():
    batch_wrapper = _read("scripts/build_solin_appimage.bat")
    powershell_wrapper = _read("scripts/build_solin_appimage.ps1")

    assert "build_solin_appimage.ps1" in batch_wrapper
    assert "exit /b %ERRORLEVEL%" in batch_wrapper
    assert '"--distribution", $Distribution' in powershell_wrapper
    assert '$Distribution = "Ubuntu-26.04"' in powershell_wrapper
    assert '"--cd", $ProjectRoot' in powershell_wrapper
    assert "SOLIN_APPIMAGE_SKIP_BUILD=1" in powershell_wrapper
    assert '"xvfb-run", "--auto-servernum"' in powershell_wrapper
    assert "$LASTEXITCODE -ne 0" in powershell_wrapper


def test_linux_ci_installs_all_pinned_obs_direct_dependency_providers():
    script = _read("scripts/build_solin.sh")
    packages = re.search(r"libobs_runtime_packages=\(\n(.*?)\n\)", script, re.DOTALL)
    assert packages is not None
    declared = packages[1].split()
    # Direct DT_NEEDED providers inspected in the official OBS 33 Ubuntu 26.04
    # asset and published wheel; libavfilter also serves Solin's native playback.
    # libc6 supplies the loader, libc and libm; apt resolves transitive providers.
    required = {
        "libasound2t64", "libavcodec62", "libavdevice62", "libavfilter11", "libavformat62", "libavutil60",
        "libc6", "libcurl4t64", "libdrm2", "libegl1", "libfontconfig1", "libfreetype6",
        "libgcc-s1", "libglib2.0-0t64", "libjansson4", "libmbedcrypto16",
        "libmbedtls21", "libmbedx509-7", "libpci3", "libpipewire-0.3-0t64",
        "libpulse0", "librist4", "libspeexdsp1", "libsrt1.5-openssl", "libstdc++6",
        "libswresample6", "libswscale9", "libudev1", "libuuid1", "libv4l-0t64",
        "libva-drm2", "libva2", "libvpl2", "libwayland-client0", "libwayland-egl1",
        "libx11-6", "libx11-xcb1", "libx264-165", "libxcb-composite0", "libxcb-randr0",
        "libxcb-shm0", "libxcb-xfixes0", "libxcb-xinerama0", "libxcb1", "libxkbcommon0",
        "zlib1g",
    }
    assert set(declared) == required
    assert len(declared) == len(required)
    assert script.index("--print-native-packages") < script.index('PYTHON="$(resolve_python)"')
    for relative in (".github/workflows/build-solin-linux.yml", ".github/workflows/quality.yml"):
        workflow = _read(relative)
        assert "bash scripts/build_solin.sh --print-native-packages" in workflow
        assert '"${libobs_packages[@]}"' in workflow
        assert "add-apt-repository" not in workflow


def test_linux_ci_bounds_package_mirror_failures():
    for relative, step_name in (
        (".github/workflows/build-solin-linux.yml", "Install Linux build and runtime dependencies"),
        (".github/workflows/quality.yml", "Install Linux runtime dependencies"),
    ):
        workflow = _read(relative)
        block = workflow.split(f"- name: {step_name}", 1)[1].split("- name:", 1)[0]
        assert "timeout-minutes: 20" in block
        assert "Acquire::Retries=3" in block
        assert "Acquire::http::Timeout=30" in block
        assert "Acquire::https::Timeout=30" in block
        assert 'sudo apt-get "${apt_options[@]}" update' in block
        assert 'sudo apt-get "${apt_options[@]}" install' in block


def test_linux_build_and_platform_tests_use_the_reference_runner():
    build = yaml.load(_read(".github/workflows/build-solin-linux.yml"), Loader=yaml.BaseLoader)
    assert build["jobs"]["build"]["runs-on"] == "ubuntu-26.04"
    quality = yaml.load(_read(".github/workflows/quality.yml"), Loader=yaml.BaseLoader)
    platforms = quality["jobs"]["platforms"]
    linux = [row for row in platforms["strategy"]["matrix"]["include"] if row["platform"] == "Linux"]
    assert len(linux) == 1
    assert linux[0]["runner"] == "ubuntu-26.04"
    assert platforms["runs-on"] == "${{ matrix.runner }}"


def test_workflow_validator_explicitly_recognizes_the_new_runner_label():
    quality = yaml.load(_read(".github/workflows/quality.yml"), Loader=yaml.BaseLoader)
    validator = next(step["run"] for step in quality["jobs"]["static"]["steps"]
                     if step.get("name") == "Validate workflows")
    # actionlint 1.7.12 predates this hosted label. Allow it explicitly without
    # suppressing diagnostics for other unknown runner labels.
    config = validator.split("<<'YAML'\n", 1)[1].split("\nYAML", 1)[0]
    build = yaml.load(_read(".github/workflows/build-solin-linux.yml"), Loader=yaml.BaseLoader)
    assert yaml.safe_load(config)["self-hosted-runner"]["labels"] == [build["jobs"]["build"]["runs-on"]]
    assert '-config-file "$config"' in validator
    assert "-ignore" not in validator


def test_linux_mux_comes_from_the_checksum_pinned_official_release():
    script = _read("scripts/build_solin.sh")
    assert 'OBS_RUNTIME_VERSION="33.0.0"' in script
    assert 'OBS_RUNTIME_SHA256="f5844914eadd560314f3a9db020afa7a8afbefb67a26c488f2956dc339704918"' in script
    assert 'OBS-Studio-${OBS_RUNTIME_VERSION}-Ubuntu-26.04-x86_64.deb' in script
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
    from solin.core.releases.manifest import REQUIRED_ASSETS

    script = _read("scripts/build_solin.sh")
    assert 'platform.machine() != "x86_64"' in script
    floor = tuple(map(int, REQUIRED_ASSETS[("linux", "x86_64", "appimage")][1].split(".")))
    assert floor == (2, 43)
    assert f'tuple(map(int, version.split("."))) < {floor}' in script
    assert script.index('tuple(map(int, version.split(".")))') < script.index('"${PYTHON}" scripts/build_media_extract.py')


@pytest.mark.parametrize("machine,libc,version,python_version,supported", [
    ("x86_64", "glibc", "2.43", (3, 13), True),
    ("x86_64", "glibc", "2.44", (3, 14), True),
    ("x86_64", "glibc", "2.42", (3, 13), False),
    ("x86_64", "musl", "1.2.5", (3, 13), False),
    ("aarch64", "glibc", "2.43", (3, 13), False),
    ("x86_64", "glibc", "2.43", (3, 12), False),
])
def test_linux_build_platform_guard(machine, libc, version, python_version, supported, monkeypatch):
    import platform

    script = _read("scripts/build_solin.sh")
    guard = script.split('"${PYTHON}" - <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    with monkeypatch.context() as patch:
        patch.setattr(platform, "machine", lambda: machine)
        patch.setattr(platform, "libc_ver", lambda: (libc, version))
        patch.setattr(sys, "version_info", python_version)
        if supported:
            exec(compile(guard, "build_solin platform guard", "exec"), {})
        else:
            with pytest.raises(SystemExit):
                exec(compile(guard, "build_solin platform guard", "exec"), {})
