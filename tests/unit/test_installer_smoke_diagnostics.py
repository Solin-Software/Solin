from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e import test_installer_upgrade_smoke as smoke


@pytest.mark.parametrize("scope", ["user", "machine"])
@pytest.mark.parametrize("failed_architecture", [None, "x64", "x86"])
def test_installed_camera_uses_production_broker_and_reports_verification_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope, failed_architecture
) -> None:
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = {}
    for architecture in smoke._PE_MACHINES:
        path = tmp_path / architecture / "camera.dll"
        path.parent.mkdir()
        path.touch()
        paths[architecture] = path
    monkeypatch.setattr(smoke, "_camera_registration", lambda arch, _scope: (
        paths[arch], "Both", "Solin Virtual Camera", b"filter-data",
    ))
    monkeypatch.setattr(smoke, "_pe_machine", lambda path: smoke._PE_MACHINES[path.parent.name])
    harnesses = {arch: tmp_path / f"{arch}-harness.exe" for arch in paths}
    for architecture, harness in harnesses.items():
        monkeypatch.setenv(f"SOLIN_DIRECTSHOW_HARNESS_{architecture.upper()}", str(harness))
        harness.touch()
    environment = {"fixture": "installed-camera"}
    calls = []

    def verify(command, **options):
        calls.append(command)
        assert options["env"] is environment
        architecture = Path(command[1]).parent.name
        failed = architecture == failed_architecture
        return subprocess.CompletedProcess(
            command, int(failed), stdout="qualification output", stderr="broker diagnostic",
        )

    monkeypatch.setattr(smoke.subprocess, "run", verify)
    if failed_architecture:
        with pytest.raises(pytest.fail.Exception, match=rf"{failed_architecture} installed") as failure:
            smoke._assert_camera_registered(environment, scope)
        assert "qualification output" in str(failure.value)
        assert "broker diagnostic" in str(failure.value)
    else:
        smoke._assert_camera_registered(environment, scope)
    expected = ["x64"] if failed_architecture == "x64" else ["x64", "x86"]
    assert calls == [[str(harnesses[arch]), str(paths[arch]), "--production-broker"]
                     for arch in expected]


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
