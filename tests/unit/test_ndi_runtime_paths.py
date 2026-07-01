from __future__ import annotations

from solin.core.integrations.ndi_runtime_paths import candidate_ndi_library_paths


def test_candidate_ndi_library_paths_prefers_bare_library_names_first():
    paths = candidate_ndi_library_paths({})

    assert paths[:2] == [
        "Processing.NDI.Lib.x64.dll",
        "Processing.NDI.Lib.x86.dll",
    ]


def test_candidate_ndi_library_paths_includes_configured_runtime_dirs():
    paths = candidate_ndi_library_paths(
        {
            "NDI_RUNTIME_DIR_V6": r"D:\NDI6",
            "NDI_RUNTIME_DIR": r"E:\NDI",
            "ProgramFiles": r"C:\Apps",
            "ProgramFiles(x86)": r"C:\Apps32",
        }
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
