from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


def path_from_env(name: str, *, purpose: str) -> Path:
    raw_path = os.environ.get(name, "").strip()
    if not raw_path:
        pytest.skip(f"Set {name} to run {purpose}.")
    path = Path(raw_path)
    if not path.is_file():
        pytest.fail(f"{name} does not point to a file: {path}")
    return path


def isolated_app_env(temp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["APPDATA"] = str(temp_root / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(temp_root / "AppData" / "Local")
    env.setdefault(
        "QT_LOGGING_RULES",
        "qt.qpa.mime=false;qt.multimedia.ffmpeg=false",
    )
    return env


def assert_process_survives_startup(
    exe_path: Path,
    *,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> None:
    startup_seconds = float(os.environ.get("SOLIN_E2E_STARTUP_SECONDS", "8"))
    shutdown_timeout = float(os.environ.get("SOLIN_E2E_SHUTDOWN_TIMEOUT", "10"))

    process = subprocess.Popen(  # noqa: S603 - e2e runs a user-supplied app artifact
        [str(exe_path), *args],
        cwd=exe_path.parent,
        env=env,
    )
    try:
        deadline = time.monotonic() + startup_seconds
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                pytest.fail(f"Packaged app exited during startup with {exit_code}.")
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("Packaged app did not stop after the smoke test.")
