from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]


def _pair_id(x64: bytes, x86: bytes) -> str:
    digests = f"{hashlib.sha256(x64).hexdigest()}:{hashlib.sha256(x86).hexdigest()}"
    return hashlib.sha256(digests.encode("ascii")).hexdigest()[:20]


def _compile_staging_probe(
    root: Path,
    storage: Path,
    x64: bytes,
    x86: bytes,
    *,
    rollback: bool = False,
    preparation_failure: bool = False,
) -> Path:
    if os.name != "nt":
        pytest.skip("The Inno staging contract requires Windows.")
    compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("The Inno staging contract requires Inno Setup 6.")
    root.mkdir(parents=True)
    (root / "x64.dll").write_bytes(x64)
    (root / "x86.dll").write_bytes(x86)
    coordinator = (_ROOT / "packaging/windows/installer/virtual_camera_registration.iss").read_text(
        encoding="utf-8"
    )
    # Exercise the real Pascal coordinator in a private directory. This probe
    # exits in InitializeSetup, before installation or any registration call.
    for prefix in ("{commonpf64}", "{localappdata}"):
        storage_expression = f"ExpandConstant('{prefix}\\Solin\\VirtualCamera')"
        assert coordinator.count(storage_expression) == 1, "camera storage isolation must be exact"
        private_storage = str(storage).replace("'", "''")
        coordinator = coordinator.replace(storage_expression, f"'{private_storage}'")
        assert storage_expression not in coordinator, "probe must not resolve real camera storage"
    (root / "coordinator.iss").write_text(coordinator, encoding="utf-8")
    rollback_code = "RollbackCameraRegistrationTransaction();" if rollback else ""
    script = r"""
#define ProbeCodeSourcePath StringChange(SourcePath, "'", "''")
[Setup]
AppName=Camera Staging Contract
AppVersion=26.32.0.3
DefaultDirName={#SourcePath}\unused
PrivilegesRequired=lowest
Uninstallable=no
CreateUninstallRegKey=no
OutputDir=.
OutputBaseFilename=probe
[Files]
Source: "x64.dll"; DestName: "solin-virtual-camera-x64.dll"; Flags: dontcopy
Source: "x86.dll"; DestName: "solin-virtual-camera-x86.dll"; Flags: dontcopy
[Code]
function IsCameraMachineInstall(): Boolean;
begin Result := False; end;
procedure MaybeInjectVirtualCameraX86RegistrationFailure();
begin end;
#include "coordinator.iss"
function InitializeSetup(): Boolean;
begin
  try
    PrepareCameraFilterPair();
    StageCameraFilter('x64', GCameraCreatedX64File);
    StageCameraFilter('x86', GCameraCreatedX86File);
    __ROLLBACK__
    SaveStringToFile('{#ProbeCodeSourcePath}\result.txt', 'ok:' + GCameraPairId, False);
  except
    RollbackCameraRegistrationTransaction();
    SaveStringToFile('{#ProbeCodeSourcePath}\result.txt', 'error:' + GetExceptionMessage(), False);
  end;
  Result := False;
end;
"""
    script = script.replace("__ROLLBACK__", rollback_code)
    if preparation_failure:
        script = script.replace(
            "PrepareCameraFilterPair();", "RaiseException('Injected pair preparation failure.');"
        )
    (root / "probe.iss").write_text(script, encoding="utf-8")
    compiled = subprocess.run(  # noqa: S603 - trusted installed Inno compiler
        [str(compiler), "/Q", str(root / "probe.iss")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return root / "probe.exe"


def _run_staging_probe(probe: Path) -> str:
    result = subprocess.run(  # noqa: S603 - locally compiled InitializeSetup-only probe
        [str(probe), "/VERYSILENT", "/SUPPRESSMSGBOXES"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, "The probe must exit before installation"
    return (probe.parent / "result.txt").read_text()


def test_camera_rebuild_identity_and_locked_reinstall(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    x64, x86 = b"first x64 build", b"first x86 build"
    first_id = _pair_id(x64, x86)
    first = _compile_staging_probe(tmp_path / "first", storage, x64, x86)
    assert _run_staging_probe(first) == f"ok:{first_id}"
    first_x64 = storage / "versions" / first_id / "x64" / "solin-virtual-camera.dll"
    # Python's Windows file handle denies FILE_SHARE_DELETE, like a loaded DLL.
    # The second build has the same app version and changes only one architecture.
    with first_x64.open("rb"):
        with pytest.raises(PermissionError):
            first_x64.unlink()
        assert _run_staging_probe(first) == f"ok:{first_id}"
        replacement_x64 = b"second x64 build"
        second_id = _pair_id(replacement_x64, x86)
        replacement = _compile_staging_probe(tmp_path / "second", storage, replacement_x64, x86)
        assert _run_staging_probe(replacement) == f"ok:{second_id}"
        assert first_x64.read_bytes() == x64
        for architecture, expected in (("x64", replacement_x64), ("x86", x86)):
            installed = storage / "versions" / second_id / architecture / "solin-virtual-camera.dll"
            assert installed.read_bytes() == expected


def test_camera_staging_rollback_preserves_reused_pair(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    x64, x86 = b"stable x64", b"stable x86"
    pair_id = _pair_id(x64, x86)
    first = _compile_staging_probe(tmp_path / "first", storage, x64, x86)
    assert _run_staging_probe(first) == f"ok:{pair_id}"
    reused = _compile_staging_probe(tmp_path / "reuse", storage, x64, x86, rollback=True)
    assert _run_staging_probe(reused) == f"ok:{pair_id}"
    assert (storage / "versions" / pair_id / "x64" / "solin-virtual-camera.dll").read_bytes() == x64
    replacement_x86 = b"replacement x86"
    replacement_id = _pair_id(x64, replacement_x86)
    replacement = _compile_staging_probe(
        tmp_path / "replace", storage, x64, replacement_x86, rollback=True
    )
    assert _run_staging_probe(replacement) == f"ok:{replacement_id}"
    assert not (storage / "versions" / replacement_id).exists()
    assert (storage / "versions" / pair_id / "x86" / "solin-virtual-camera.dll").read_bytes() == x86


def test_camera_identity_rejects_inconsistent_existing_contents(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    x64, x86 = b"expected x64", b"expected x86"
    pair_id = _pair_id(x64, x86)
    corrupt = storage / "versions" / pair_id / "x86" / "solin-virtual-camera.dll"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"different binary or hash-prefix collision")
    probe = _compile_staging_probe(tmp_path / "probe", storage, x64, x86)
    assert _run_staging_probe(probe).startswith("error:The managed virtual-camera binary")
    assert corrupt.read_bytes() == b"different binary or hash-prefix collision"
    assert not (storage / "versions" / pair_id / "x64" / "solin-virtual-camera.dll").exists()


def test_camera_preparation_failure_does_not_clean_unowned_directories(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    directories = [storage / "versions" / architecture for architecture in ("x64", "x86")]
    for directory in directories:
        directory.mkdir(parents=True)
    probe = _compile_staging_probe(
        tmp_path / "probe", storage, b"x64", b"x86", preparation_failure=True
    )
    assert _run_staging_probe(probe) == "error:Injected pair preparation failure."
    assert all(directory.is_dir() for directory in directories)
