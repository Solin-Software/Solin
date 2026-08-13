from __future__ import annotations

import re

from solin.core.scenes.ipc_protocol import MAXIMUM_CONTROL_FRAME_BYTES, PROTOCOL_VERSION
from solin.core.scenes.model import SCHEMA_VERSION, SENSITIVE_URI_QUERY_FRAGMENTS
from tests._paths import REPO_ROOT


NATIVE_ROOT = REPO_ROOT / "native" / "media_engine"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_native_media_engine_is_an_isolated_cmake_cxx20_process() -> None:
    cmake = _read("native/media_engine/CMakeLists.txt")

    assert "cxx_std_20" in cmake
    assert "/W4 /WX /permissive-" in cmake
    assert "add_executable(solin-media-engine" in cmake
    assert "add_test(NAME frame-codec" in cmake
    assert "add_test(NAME scene-snapshot" in cmake
    assert "add_test(NAME executable-self-test" in cmake
    assert "libobs" not in cmake.casefold()
    assert "pylibobs" not in cmake.casefold()


def test_python_and_native_protocol_limits_and_versions_match() -> None:
    frame_header = _read("native/media_engine/include/solin/media_engine/frame_codec.hpp")
    control_header = _read("native/media_engine/include/solin/media_engine/control_protocol.hpp")

    assert MAXIMUM_CONTROL_FRAME_BYTES == 8 * 1024 * 1024
    assert "8U * 1024U * 1024U" in frame_header
    version = re.search(r"kControlProtocolVersion\s*=\s*(\d+)U", control_header)
    assert version is not None
    assert int(version.group(1)) == PROTOCOL_VERSION


def test_python_and_native_scene_schema_versions_match() -> None:
    native_parser = _read("native/media_engine/src/scene_snapshot.cpp")

    version = re.search(r"kSceneSchemaVersion\s*=\s*(\d+)U", native_parser)
    assert version is not None
    assert int(version.group(1)) == SCHEMA_VERSION


def test_native_dependency_downloads_are_versioned_and_digest_pinned() -> None:
    cmake = _read("native/media_engine/CMakeLists.txt")
    virtual_camera_cmake = _read(
        "native/media_engine/cmake/SolinVirtualCameraFilter.cmake"
    )
    bootstrap = _read("scripts/install_gstreamer_windows.ps1")

    assert "nlohmann/json/releases/download/v3.12.0/json.tar.xz" in cmake
    assert "URL_HASH SHA256=" in cmake
    assert '$version = "1.28.5"' in bootstrap
    assert (
        '$expectedSha256 = "51ee5eaec33008e8409d8cf6f6884457f22aa3bd515f8856f993a3eaab903530"'
        in bootstrap
    )
    assert "/CURRENTUSER" in bootstrap
    assert "Get-FileHash" in bootstrap
    assert "Windows-classic-samples/archive/d59e5f1" in virtual_camera_cmake
    assert "libyuv/archive/eb6e7bb6" in virtual_camera_cmake
    assert virtual_camera_cmake.count("URL_HASH SHA256=") == 2


def test_windows_release_builds_and_packages_the_native_engine() -> None:
    workflow = _read(".github/workflows/build-solin-windows.yml")
    build_script = _read("scripts/build_native_engine.py")
    package_script = _read("scripts/package_native_engine_windows.py")

    assert "python scripts\\build_native_engine.py --configuration Release" in workflow
    assert "python scripts\\package_native_engine_windows.py" in workflow
    assert "-InstallType runtime" in workflow
    assert "native\\media-engine\\solin-media-engine.exe" in workflow
    assert "virtual-camera\\x64\\solin-virtual-camera.dll" in workflow
    assert "virtual-camera\\x86\\solin-virtual-camera.dll" in workflow
    assert "SOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER=ON" in build_script
    assert "GSTREAMER_PLUGIN_FILENAMES = (" in package_script
    assert '"gstd3d11.dll"' in package_script
    assert "_copy_required_runtime_dlls" in package_script
    assert "_audit_plugin_licenses" in package_script
    assert "FORBIDDEN_GSTREAMER_FILENAMES" in package_script
    assert "FORBIDDEN_DEVELOPMENT_SUFFIXES" in package_script
    assert "DEFAULT_VIRTUAL_CAMERA_FILTER_X64" in package_script
    assert "DEFAULT_VIRTUAL_CAMERA_FILTER_X86" in package_script
    assert "PE_MACHINE_X64" in package_script
    assert "PE_MACHINE_X86" in package_script
    assert "sign_windows_artifacts" in workflow
    assert "default: true" in workflow
    assert "SOLIN_INNO_SIGN_WRAPPER" in workflow
    assert "unsigned-diagnostic" in workflow
    assert " verify /pa /v " in workflow


def test_windows_virtual_camera_registration_contract_is_consistent() -> None:
    contract = _read(
        "native/media_engine/include/solin/media_engine/windows_virtual_camera_contract.hpp"
    )
    installer = _read("packaging/windows/installer/setup.iss")
    patch_installer = _read("packaging/windows/installer/patch.iss")
    registration_coordinator = _read(
        "packaging/windows/installer/virtual_camera_registration.iss"
    )
    development_registration = _read("scripts/manage_virtual_camera_directshow.py")
    source_exports = _read(
        "native/media_engine/windows/directshow_virtual_camera/solin_virtual_camera.def"
    )
    local_build = _read("scripts/build_solin.bat")

    clsid = "08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F"
    assert "0x08AFA2E5" in contract
    assert clsid in development_registration
    assert "ArchitecturesAllowed=x64os" in installer
    assert "{localappdata}\\Solin\\VirtualCamera\\versions" in installer
    assert "regserver 64bit" in installer
    assert "regserver 32bit" in installer
    assert "regserver 64bit" in patch_installer
    assert "regserver 32bit" in patch_installer
    assert "MinVersion=10.0.17763" in installer
    assert "MinVersion=10.0.17763" in patch_installer
    assert "CleanupObsoleteCameraVersions" in installer
    assert "CleanupObsoleteCameraVersions" in patch_installer
    assert "{commoncf64}" not in installer
    assert "FrameServer" not in installer
    assert "SignedUninstaller=yes" in installer
    assert "SignTool={#MySignToolName}" in installer
    assert "LOCALAPPDATA" in development_registration
    assert "_stage_filters" in development_registration
    assert "System32" in development_registration
    assert "SysWOW64" in development_registration
    assert "DllUnregisterServer" in source_exports
    assert "regsvr32.exe" in registration_coordinator
    assert "RollbackCameraRegistrationTransaction" in registration_coordinator
    assert 'include "virtual_camera_registration.iss"' in installer
    assert 'include "virtual_camera_registration.iss"' in patch_installer
    assert 'command.append("/u")' in development_registration
    assert "scripts\\build_native_engine.py" in local_build
    assert "scripts\\package_native_engine_windows.py" in local_build


def test_native_source_does_not_link_or_load_the_obs_engine() -> None:
    native_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in NATIVE_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".cpp", ".hpp", ".txt"}
    ).casefold()

    assert "obs_module" not in native_source
    assert "obs_startup" not in native_source
    assert "libobs.dll" not in native_source


def test_python_and_native_scene_uri_credential_policies_match() -> None:
    native_parser = _read("native/media_engine/src/scene_snapshot.cpp")

    for fragment in SENSITIVE_URI_QUERY_FRAGMENTS:
        assert f'"{fragment}"' in native_parser
