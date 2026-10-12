from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from scripts import _windows_pe, build_virtual_camera, package_virtual_camera_windows


def _touch(path: Path, content: bytes = b"test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_pe(
    path: Path, machine: int, *, imported: str = "KERNEL32.dll", delay_import: bool = False
) -> None:
    binary = bytearray(0x500)
    binary[:2] = b"MZ"
    struct.pack_into("<I", binary, 0x3C, 0x80)
    binary[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    optional_size = 0xF0 if machine == 0x8664 else 0xE0
    struct.pack_into("<HH", binary, coff, machine, 1)
    struct.pack_into("<H", binary, coff + 16, optional_size)
    optional = coff + 20
    struct.pack_into("<H", binary, optional, 0x20B if machine == 0x8664 else 0x10B)
    count_offset, directory_offset = (108, 112) if machine == 0x8664 else (92, 96)
    struct.pack_into("<I", binary, optional + count_offset, 16)
    index = 13 if delay_import else 1
    struct.pack_into("<II", binary, optional + directory_offset + index * 8, 0x1000, 64)
    section = optional + optional_size
    binary[section : section + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", binary, section + 8, 0x300, 0x1000, 0x300, 0x200)
    if delay_import:
        struct.pack_into("<IIIIIIII", binary, 0x200, 1, 0x1080, 0, 0, 0, 0, 0, 0)
    else:
        struct.pack_into("<IIIII", binary, 0x200, 0, 0, 0, 0x1080, 0)
    name = imported.encode("ascii") + b"\0"
    binary[0x280 : 0x280 + len(name)] = name
    _touch(path, bytes(binary))


@pytest.fixture
def camera_package(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setattr(package_virtual_camera_windows.sys, "platform", "win32")
    x64 = tmp_path / "build64" / "solin-virtual-camera.dll"
    x86 = tmp_path / "build32" / "solin-virtual-camera.dll"
    application = tmp_path / "application"
    _write_pe(x64, 0x8664)
    _write_pe(x86, 0x014C)
    _touch(application / "Solin.exe")
    repository = tmp_path / "repository"
    for name in package_virtual_camera_windows.LICENSE_FILENAMES:
        _touch(repository / "native" / "virtual_camera" / "third_party" / name, name.encode())
    monkeypatch.setattr(package_virtual_camera_windows, "REPOSITORY_ROOT", repository)
    return dict(
        virtual_camera_filter_x64=x64,
        virtual_camera_filter_x86=x86,
        application_dir=application,
        verify=True,
    )


def test_release_stages_only_filter_pair_and_notices_with_a_verified_manifest(
    camera_package,
) -> None:
    destination = package_virtual_camera_windows.package_virtual_camera(**camera_package)
    assert destination == camera_package["application_dir"] / "native" / "virtual-camera"
    package_virtual_camera_windows._verify_staged_camera(destination)
    manifest = json.loads((destination / "runtime-manifest.json").read_text())
    assert manifest["filters"]["x64"]["machine"] == 0x8664
    assert manifest["filters"]["x86"]["machine"] == 0x014C
    for architecture in ("x64", "x86"):
        path = destination / architecture / "solin-virtual-camera.dll"
        assert (
            manifest["filters"][architecture]["sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        assert manifest["filters"][architecture]["imports"] == ["kernel32.dll"]
    assert len(list(destination.rglob("*.dll"))) == 2
    assert not list(destination.rglob("*.exe"))
    assert not list(destination.rglob("*.pyd"))
    assert len(manifest["licenses"]) == 2


@pytest.mark.parametrize("machine", [0x8664, 0x014C])
@pytest.mark.parametrize("delay_import", [False, True])
def test_pe_reader_audits_both_architectures_and_delay_imports(
    tmp_path: Path, machine: int, delay_import: bool
) -> None:
    dll = tmp_path / "solin-virtual-camera.dll"
    _write_pe(dll, machine, delay_import=delay_import)
    assert _windows_pe.pe_imports(dll) == {"KERNEL32.dll"}
    assert _windows_pe.pe_machine(dll) == machine


@pytest.mark.parametrize("imported", ["VCRUNTIME140.dll", "gstvideo-1.0-0.dll", "unexpected.dll"])
def test_filter_rejects_imports_outside_the_system_allowlist(tmp_path: Path, imported: str) -> None:
    dll = tmp_path / "solin-virtual-camera.dll"
    _write_pe(dll, 0x8664, imported=imported, delay_import=True)
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError,
        match="outside the production allowlist",
    ):
        package_virtual_camera_windows._validate_filter_imports(dll, "x64")


def test_filter_import_audit_fails_closed(tmp_path: Path) -> None:
    dll = tmp_path / "solin-virtual-camera.dll"
    _touch(dll)
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="could not be audited"
    ):
        package_virtual_camera_windows._validate_filter_imports(dll, "x64")


@pytest.mark.parametrize("architecture", ["x64", "x86"])
def test_skip_verification_still_rejects_wrong_architecture(
    camera_package, architecture: str
) -> None:
    _write_pe(
        camera_package[f"virtual_camera_filter_{architecture}"],
        0x014C if architecture == "x64" else 0x8664,
    )
    camera_package["verify"] = False
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="wrong PE architecture"
    ):
        package_virtual_camera_windows.package_virtual_camera(**camera_package)
    assert not (camera_package["application_dir"] / "native").exists()


def test_skip_verification_still_rejects_forbidden_import(camera_package) -> None:
    _write_pe(camera_package["virtual_camera_filter_x86"], 0x014C, imported="VCRUNTIME140.dll")
    camera_package["verify"] = False
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="allowlist"
    ):
        package_virtual_camera_windows.package_virtual_camera(**camera_package)


def test_manifest_detects_modified_binary_and_unexpected_files(camera_package) -> None:
    destination = package_virtual_camera_windows.package_virtual_camera(**camera_package)
    _touch(destination / "unexpected.pyd")
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="unexpected files"
    ):
        package_virtual_camera_windows._verify_staged_camera(destination)
    (destination / "unexpected.pyd").unlink()
    with (destination / "x64" / "solin-virtual-camera.dll").open("ab") as dll:
        dll.write(b"tampered")
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="manifest"
    ):
        package_virtual_camera_windows._verify_staged_camera(destination)


def test_staged_verification_failure_preserves_previous_payload_and_cleans_staging(
    camera_package, monkeypatch
) -> None:
    destination = package_virtual_camera_windows.package_virtual_camera(**camera_package)
    _touch(destination / "previous")

    def fail_verification(_root: Path) -> None:
        raise package_virtual_camera_windows.VirtualCameraPackagingError("verification failed")

    monkeypatch.setattr(package_virtual_camera_windows, "_verify_staged_camera", fail_verification)
    with pytest.raises(
        package_virtual_camera_windows.VirtualCameraPackagingError, match="verification failed"
    ):
        package_virtual_camera_windows.package_virtual_camera(**camera_package)
    assert (destination / "previous").is_file()
    assert list(destination.parent.iterdir()) == [destination]


def test_missing_license_preserves_previous_payload(camera_package, monkeypatch) -> None:
    destination = package_virtual_camera_windows.package_virtual_camera(**camera_package)
    _touch(destination / "previous")
    source = (
        package_virtual_camera_windows.REPOSITORY_ROOT / "native" / "virtual_camera" / "third_party"
    )
    (source / "libyuv.LICENSE").unlink()
    with pytest.raises(FileNotFoundError):
        package_virtual_camera_windows.package_virtual_camera(**camera_package)
    assert (destination / "previous").is_file()
    assert list(destination.parent.iterdir()) == [destination]


def test_cli_defaults_use_matching_release_filter_paths() -> None:
    options = package_virtual_camera_windows._parser().parse_args([])
    assert (
        options.virtual_camera_filter_x64
        == package_virtual_camera_windows.DEFAULT_VIRTUAL_CAMERA_FILTER_X64
    )
    assert (
        options.virtual_camera_filter_x86
        == package_virtual_camera_windows.DEFAULT_VIRTUAL_CAMERA_FILTER_X86
    )
    assert options.virtual_camera_filter_x64.parts[-3:] == (
        "virtual-camera-x64",
        "Release",
        "solin-virtual-camera.dll",
    )
    assert options.virtual_camera_filter_x86.parts[-3:] == (
        "virtual-camera-x86",
        "Release",
        "solin-virtual-camera.dll",
    )
    options = package_virtual_camera_windows._parser().parse_args(
        [
            "--virtual-camera-filter-x64",
            "custom64.dll",
            "--virtual-camera-filter-x86",
            "custom32.dll",
        ]
    )
    assert options.virtual_camera_filter_x64 == Path("custom64.dll")
    assert options.virtual_camera_filter_x86 == Path("custom32.dll")


def test_staging_rename_retries_a_transient_windows_file_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    original_replace = Path.replace
    attempts = 0

    def flaky_replace(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(package_virtual_camera_windows.time, "sleep", lambda _delay: None)

    package_virtual_camera_windows._replace_staging_directory(staging, destination)

    assert attempts == 2
    assert destination.is_dir()


def test_staging_replacement_restores_previous_payload_on_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    destination.mkdir()
    _touch(staging / "payload", b"new")
    _touch(destination / "payload", b"previous")
    original_replace = Path.replace

    def fail_new_payload(path: Path, target: Path) -> Path:
        if path == staging:
            raise OSError("simulated replacement failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_new_payload)

    try:
        package_virtual_camera_windows._replace_staging_directory(staging, destination)
    except OSError as error:
        assert str(error) == "simulated replacement failure"
    else:
        raise AssertionError("replacement failure was not propagated")

    assert (destination / "payload").read_bytes() == b"previous"


def test_staging_cleanup_retries_a_transient_windows_file_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    original_rmtree = package_virtual_camera_windows.shutil.rmtree
    attempts = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        original_rmtree(path)

    monkeypatch.setattr(package_virtual_camera_windows.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(package_virtual_camera_windows.time, "sleep", lambda _delay: None)

    package_virtual_camera_windows._remove_backup_with_retry(backup)

    assert attempts == 2
    assert not backup.exists()


@pytest.mark.parametrize("run_tests", [True, False])
def test_build_configures_each_architecture_and_runs_only_requested_tests(
    tmp_path: Path, monkeypatch, run_tests: bool
) -> None:
    monkeypatch.setattr(build_virtual_camera.sys, "platform", "win32")
    monkeypatch.setattr(build_virtual_camera.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(build_virtual_camera, "_require_command", lambda name: name)
    x64, x86 = tmp_path / "x64", tmp_path / "x86"
    _write_pe(x64 / "Release" / "solin-virtual-camera.dll", 0x8664)
    _write_pe(x86 / "Release" / "solin-virtual-camera.dll", 0x014C)
    commands = []
    monkeypatch.setattr(build_virtual_camera, "_run", commands.append)
    result = build_virtual_camera.build_virtual_camera(
        configuration="Release",
        build_x64_directory=x64,
        build_x86_directory=x86,
        run_tests=run_tests,
        jobs=2,
    )
    assert result == (
        x64 / "Release" / "solin-virtual-camera.dll",
        x86 / "Release" / "solin-virtual-camera.dll",
    )
    configure = [command for command in commands if "-S" in command]
    assert [command[command.index("-A") + 1] for command in configure] == ["x64", "Win32"]
    assert all(
        f"-DSOLIN_VIRTUAL_CAMERA_BUILD_TESTS={'ON' if run_tests else 'OFF'}" in command
        for command in configure
    )
    assert sum(command[0] == "ctest" for command in commands) == (2 if run_tests else 0)
    assert all(command[-2:] == ["--parallel", "2"] for command in commands if "--build" in command)


def test_build_rejects_shared_build_directory_before_running_cmake(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(build_virtual_camera.sys, "platform", "win32")
    monkeypatch.setattr(build_virtual_camera.platform, "machine", lambda: "AMD64")
    with pytest.raises(build_virtual_camera.VirtualCameraBuildError, match="separate directories"):
        build_virtual_camera.build_virtual_camera(
            configuration="Release",
            build_x64_directory=tmp_path,
            build_x86_directory=tmp_path,
            run_tests=True,
            jobs=0,
        )
