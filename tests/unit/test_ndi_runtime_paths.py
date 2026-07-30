from __future__ import annotations

from solin.core.integrations.ndi_runtime_paths import candidate_ndi_library_paths


def test_windows_candidates_prefer_bare_library_names_first():
    paths = candidate_ndi_library_paths({}, platform="win32")

    assert paths[:2] == [
        "Processing.NDI.Lib.x64.dll",
        "Processing.NDI.Lib.x86.dll",
    ]


def test_windows_candidates_include_configured_runtime_dirs():
    paths = candidate_ndi_library_paths(
        {
            "NDI_RUNTIME_DIR_V6": r"D:\NDI6",
            "NDI_RUNTIME_DIR": r"E:\NDI",
            "ProgramFiles": r"C:\Apps",
            "ProgramFiles(x86)": r"C:\Apps32",
        },
        platform="win32",
    )

    assert str(r"D:\NDI6\Processing.NDI.Lib.x64.dll") in paths
    assert str(r"D:\NDI6\Bin\x64\Processing.NDI.Lib.x64.dll") in paths
    assert str(r"E:\NDI\Processing.NDI.Lib.x86.dll") in paths
    assert (
        str(r"C:\Apps\obs-studio\obs-plugins\64bit\Processing.NDI.Lib.x64.dll")
        in paths
    )
    assert (
        str(r"C:\Apps32\NDI\NDI 5 Runtime\v5\Processing.NDI.Lib.x86.dll")
        in paths
    )
    assert all("/" not in path for path in paths)


def test_linux_candidates_prefer_soname_bare_names_then_standard_dirs():
    paths = candidate_ndi_library_paths({}, platform="linux")

    assert paths[0] == "libndi.so.6"  # soname first → OS loader search
    assert "libndi.so" in paths
    assert "/usr/lib/x86_64-linux-gnu/libndi.so.6" in paths
    assert "/usr/local/lib/libndi.so.6" in paths
    assert all("\\" not in path for path in paths)


def test_linux_candidates_include_env_and_home_sdk_locations():
    paths = candidate_ndi_library_paths(
        {"NDI_RUNTIME_DIR": "/opt/custom-ndi", "HOME": "/home/op"},
        platform="linux",
    )

    assert "/opt/custom-ndi/libndi.so.6" in paths
    assert "/opt/custom-ndi/lib/libndi.so.6" in paths
    assert "/home/op/NDI SDK for Linux/lib/x86_64-linux-gnu/libndi.so.6" in paths


def test_macos_candidates_use_dylib():
    paths = candidate_ndi_library_paths({"HOME": "/Users/op"}, platform="darwin")

    assert paths[0] == "libndi.dylib"
    assert "/usr/local/lib/libndi.dylib" in paths
    assert "/opt/homebrew/lib/libndi.dylib" in paths
    assert "/Users/op/NDI SDK for Apple/lib/macOS/libndi.dylib" in paths
