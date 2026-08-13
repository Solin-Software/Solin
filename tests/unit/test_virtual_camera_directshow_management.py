from __future__ import annotations

import hashlib
import struct
import subprocess
from pathlib import Path

import pytest

from scripts import manage_virtual_camera_directshow


def _write_pe(path: Path, machine: int, payload: bytes = b"") -> None:
    binary = bytearray(0x100)
    binary[:2] = b"MZ"
    struct.pack_into("<I", binary, 0x3C, 0x80)
    binary[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", binary, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(binary + payload)


def test_pe_validation_distinguishes_x86_and_x64(tmp_path: Path) -> None:
    x64 = tmp_path / "x64" / manage_virtual_camera_directshow.FILTER_FILENAME
    x86 = tmp_path / "x86" / manage_virtual_camera_directshow.FILTER_FILENAME
    _write_pe(x64, manage_virtual_camera_directshow.PE_MACHINE_X64)
    _write_pe(x86, manage_virtual_camera_directshow.PE_MACHINE_X86)

    assert manage_virtual_camera_directshow._validate_filter(
        x64, manage_virtual_camera_directshow.PE_MACHINE_X64, "x64"
    ) == x64.resolve()
    with pytest.raises(
        manage_virtual_camera_directshow.VirtualCameraRegistrationError,
        match="wrong PE architecture",
    ):
        manage_virtual_camera_directshow._validate_filter(
            x86, manage_virtual_camera_directshow.PE_MACHINE_X64, "x64"
        )


def test_staging_uses_an_immutable_combined_content_version(
    tmp_path: Path, monkeypatch
) -> None:
    source_x64 = tmp_path / "build64" / manage_virtual_camera_directshow.FILTER_FILENAME
    source_x86 = tmp_path / "build32" / manage_virtual_camera_directshow.FILTER_FILENAME
    _write_pe(source_x64, manage_virtual_camera_directshow.PE_MACHINE_X64, b"x64")
    _write_pe(source_x86, manage_virtual_camera_directshow.PE_MACHINE_X86, b"x86")
    staging = tmp_path / "versions"
    monkeypatch.setattr(
        manage_virtual_camera_directshow, "_managed_staging_root", lambda: staging
    )

    staged_x64, staged_x86 = manage_virtual_camera_directshow._stage_filters(
        source_x64, source_x86
    )
    combined = hashlib.sha256(
        (
            hashlib.sha256(source_x64.read_bytes()).hexdigest()
            + ":"
            + hashlib.sha256(source_x86.read_bytes()).hexdigest()
        ).encode("ascii")
    ).hexdigest()[:20]

    assert staged_x64 == staging / combined / "x64" / source_x64.name
    assert staged_x86 == staging / combined / "x86" / source_x86.name
    assert staged_x64.read_bytes() == source_x64.read_bytes()
    assert staged_x86.read_bytes() == source_x86.read_bytes()


def test_registration_uses_the_matching_system_regsvr32(
    tmp_path: Path, monkeypatch
) -> None:
    system_root = tmp_path / "Windows"
    regsvr64 = system_root / "System32" / "regsvr32.exe"
    regsvr32 = system_root / "SysWOW64" / "regsvr32.exe"
    regsvr64.parent.mkdir(parents=True)
    regsvr32.parent.mkdir(parents=True)
    regsvr64.write_bytes(b"exe")
    regsvr32.write_bytes(b"exe")
    filter_path = tmp_path / manage_virtual_camera_directshow.FILTER_FILENAME
    filter_path.write_bytes(b"dll")
    commands: list[list[str]] = []
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(
        manage_virtual_camera_directshow.subprocess,
        "run",
        lambda command, **_options: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    manage_virtual_camera_directshow._run_registration(
        "x64", filter_path, unregister=False
    )
    manage_virtual_camera_directshow._run_registration(
        "x86", filter_path, unregister=True
    )

    assert commands == [
        [str(regsvr64), "/s", str(filter_path)],
        [str(regsvr32), "/s", "/u", str(filter_path)],
    ]


def test_install_rolls_back_both_views_when_x86_registration_fails(
    tmp_path: Path, monkeypatch
) -> None:
    x64 = tmp_path / "x64" / manage_virtual_camera_directshow.FILTER_FILENAME
    x86 = tmp_path / "x86" / manage_virtual_camera_directshow.FILTER_FILENAME
    _write_pe(x64, manage_virtual_camera_directshow.PE_MACHINE_X64)
    _write_pe(x86, manage_virtual_camera_directshow.PE_MACHINE_X86)
    previous_x64 = manage_virtual_camera_directshow.RegistrationState(
        "x64", False, False, error="missing"
    )
    previous_x86 = manage_virtual_camera_directshow.RegistrationState(
        "x86", False, False, error="missing"
    )
    registrations: list[tuple[str, bool]] = []
    restored: list[str] = []
    state_calls = 0

    monkeypatch.setattr(manage_virtual_camera_directshow, "_require_windows", lambda: None)
    monkeypatch.setattr(
        manage_virtual_camera_directshow,
        "status",
        lambda: (previous_x64, previous_x86),
    )
    monkeypatch.setattr(
        manage_virtual_camera_directshow,
        "_stage_filters",
        lambda _x64, _x86: (x64, x86),
    )

    def registration(architecture: str, _path: Path, *, unregister: bool) -> None:
        registrations.append((architecture, unregister))
        if architecture == "x86" and not unregister:
            raise manage_virtual_camera_directshow.VirtualCameraRegistrationError(
                "simulated x86 failure"
            )

    def state(architecture: str):
        nonlocal state_calls
        state_calls += 1
        return manage_virtual_camera_directshow.RegistrationState(
            architecture, True, True, dll_path=str(x64 if architecture == "x64" else x86)
        )

    monkeypatch.setattr(manage_virtual_camera_directshow, "_run_registration", registration)
    monkeypatch.setattr(manage_virtual_camera_directshow, "_registration_state", state)
    monkeypatch.setattr(
        manage_virtual_camera_directshow,
        "_restore_registration",
        lambda architecture, _previous: restored.append(architecture),
    )

    with pytest.raises(
        manage_virtual_camera_directshow.VirtualCameraRegistrationError,
        match="simulated x86 failure",
    ):
        manage_virtual_camera_directshow.install_filters(x64, x86)

    assert registrations == [("x64", False), ("x86", False)]
    assert restored == ["x86", "x64"]
    assert state_calls == 1


def test_cleanup_preserves_active_and_locked_versions(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "versions"
    active = root / "active"
    obsolete = root / "obsolete"
    locked = root / "locked"
    interrupted = root / ".staging-interrupted"
    for version in (active, obsolete, locked, interrupted):
        (version / "x64").mkdir(parents=True)
        (version / "x86").mkdir()
    active_path = active / "x64" / manage_virtual_camera_directshow.FILTER_FILENAME
    active_path.write_bytes(b"active")
    monkeypatch.setattr(
        manage_virtual_camera_directshow, "_managed_staging_root", lambda: root
    )
    monkeypatch.setattr(
        manage_virtual_camera_directshow,
        "_registration_state",
        lambda architecture: manage_virtual_camera_directshow.RegistrationState(
            architecture,
            architecture == "x64",
            architecture == "x64",
            dll_path=str(active_path) if architecture == "x64" else "",
        ),
    )
    original_rmtree = manage_virtual_camera_directshow.shutil.rmtree

    def remove(path: Path) -> None:
        if path == locked:
            raise PermissionError("loaded filter")
        original_rmtree(path)

    monkeypatch.setattr(manage_virtual_camera_directshow.shutil, "rmtree", remove)

    manage_virtual_camera_directshow._cleanup_staged_versions()

    assert active.is_dir()
    assert locked.is_dir()
    assert not obsolete.exists()
    assert not interrupted.exists()
