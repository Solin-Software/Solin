from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from scripts import manage_virtual_camera_source


def test_frame_server_restart_uses_bounded_noninteractive_powershell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    invocation: dict[str, object] = {}

    def run(command, **options):
        invocation["command"] = command
        invocation["options"] = options
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(manage_virtual_camera_source.subprocess, "run", run)

    manage_virtual_camera_source._restart_frame_server()

    command = invocation["command"]
    assert isinstance(command, list)
    assert command[0] == str(powershell)
    assert command[1:4] == ["-NoLogo", "-NoProfile", "-NonInteractive"]
    assert "Restart-Service" in command[-1]
    options = invocation["options"]
    assert isinstance(options, dict)
    assert options["timeout"] == 25
    assert options["check"] is False


def test_frame_server_restart_failure_reports_that_registration_succeeded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        manage_virtual_camera_source.subprocess,
        "run",
        lambda command, **options: subprocess.CompletedProcess(command, 1, "", "access denied"),
    )

    try:
        manage_virtual_camera_source._restart_frame_server()
    except manage_virtual_camera_source.VirtualCameraRegistrationError as error:
        assert "registered" in str(error)
        assert "access denied" in str(error)
    else:
        raise AssertionError("a failed service restart must be reported")


def test_staged_camera_source_name_matches_the_stabilized_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "build" / "solin-virtual-camera-source.dll"
    source.parent.mkdir()
    source.write_bytes(b"initial-build")
    staging_root = tmp_path / "protected"
    original_copy = manage_virtual_camera_source.shutil.copy2

    def replace_source_after_copy(source_path: Path, destination: Path) -> Path:
        result = original_copy(source_path, destination)
        source_path.write_bytes(b"next-build")
        return result

    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_managed_staging_root",
        lambda: staging_root,
    )
    monkeypatch.setattr(
        manage_virtual_camera_source.shutil,
        "copy2",
        replace_source_after_copy,
    )

    staged = manage_virtual_camera_source._stage_source(source)

    copied = b"initial-build"
    digest = hashlib.sha256(copied).hexdigest()[:16]
    assert staged == staging_root / f"solin-virtual-camera-source-{digest}.dll"
    assert staged.read_bytes() == copied


def test_staged_camera_source_cleanup_keeps_only_the_active_digest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging_root = tmp_path / "protected"
    staging_root.mkdir()
    active = staging_root / "solin-virtual-camera-source-active.dll"
    obsolete = staging_root / "solin-virtual-camera-source-obsolete.dll"
    unrelated = staging_root / "notes.txt"
    active.write_bytes(b"active")
    obsolete.write_bytes(b"obsolete")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_managed_staging_root",
        lambda: staging_root,
    )

    manage_virtual_camera_source._cleanup_staged_sources(keep=frozenset({active}))

    assert active.is_file()
    assert not obsolete.exists()
    assert unrelated.is_file()


def test_unregister_removes_persistent_camera_before_com_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "build" / "solin-virtual-camera-source.dll"
    source.parent.mkdir()
    source.write_bytes(b"source")
    operations: list[object] = []
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_read_registration",
        lambda: (str(tmp_path / "staged-source.dll"), "Both"),
    )
    monkeypatch.setattr(manage_virtual_camera_source, "_is_managed_path", lambda _path: True)
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_remove_persistent_camera",
        lambda path: operations.append(("camera", path)),
    )
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_delete_registration_keys",
        lambda: operations.append("registry"),
    )
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_cleanup_staged_sources",
        lambda: operations.append("staged"),
    )

    assert manage_virtual_camera_source.unregister_source(source, force=False)
    assert operations == [("camera", source), "registry", "staged"]


def test_unregister_preserves_registration_when_camera_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "build" / "solin-virtual-camera-source.dll"
    source.parent.mkdir()
    source.write_bytes(b"source")
    registration_deleted = False
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_read_registration",
        lambda: (str(tmp_path / "staged-source.dll"), "Both"),
    )
    monkeypatch.setattr(manage_virtual_camera_source, "_is_managed_path", lambda _path: True)

    def fail_cleanup(_engine: Path) -> None:
        raise manage_virtual_camera_source.VirtualCameraRegistrationError("cleanup failed")

    def delete_registration() -> None:
        nonlocal registration_deleted
        registration_deleted = True

    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_remove_persistent_camera",
        fail_cleanup,
    )
    monkeypatch.setattr(
        manage_virtual_camera_source,
        "_delete_registration_keys",
        delete_registration,
    )

    try:
        manage_virtual_camera_source.unregister_source(source, force=False)
    except manage_virtual_camera_source.VirtualCameraRegistrationError:
        pass
    else:
        raise AssertionError("cleanup failure must be reported")
    assert not registration_deleted
