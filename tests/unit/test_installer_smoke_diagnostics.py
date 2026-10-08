from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e import test_installer_upgrade_smoke as smoke


def test_installer_timeout_retains_inno_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def blocked(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        log = Path(next(arg.removeprefix("/LOG=") for arg in command if arg.startswith("/LOG=")))
        log.write_text("camera cleanup entered", encoding="utf-8-sig")
        raise subprocess.TimeoutExpired(command, 240)

    monkeypatch.setattr(smoke, "run_windows_process_tree", blocked)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        smoke._run_logged_installer_process(["private-probe.exe"], cwd=tmp_path, env={})
    assert failure.value.__notes__ == ["camera cleanup entered"]


def test_cleanup_preserves_primary_reinstall_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    camera = tmp_path / "locked-camera.dll"
    camera.write_bytes(b"residue")
    monkeypatch.setattr(smoke, "_assert_camera_absent", lambda scope: None)
    primary = subprocess.TimeoutExpired("reinstall", 240)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        try:
            raise primary
        finally:
            smoke._cleanup_installation(tmp_path, {}, "user", {"x64": camera})
    assert failure.value is primary
    assert "unloaded camera DLL survived uninstall" in primary.__notes__[0]


def test_cleanup_standalone_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def stale_registration(scope: str) -> None:
        pytest.fail("stale camera registration")

    monkeypatch.setattr(smoke, "_assert_camera_absent", stale_registration)
    with pytest.raises(pytest.fail.Exception, match="stale camera registration"):
        smoke._cleanup_installation(tmp_path, {}, "user", {})
