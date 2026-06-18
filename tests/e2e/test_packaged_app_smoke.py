from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


def _packaged_exe_from_env() -> Path:
    raw_path = os.environ.get("SOLIN_PACKAGED_EXE", "").strip()
    if not raw_path:
        pytest.skip("Set SOLIN_PACKAGED_EXE to run packaged application smoke tests.")
    path = Path(raw_path)
    if not path.is_file():
        pytest.fail(f"SOLIN_PACKAGED_EXE does not point to a file: {path}")
    return path


def test_packaged_app_survives_startup_window() -> None:
    exe_path = _packaged_exe_from_env()
    startup_seconds = float(os.environ.get("SOLIN_E2E_STARTUP_SECONDS", "8"))
    shutdown_timeout = float(os.environ.get("SOLIN_E2E_SHUTDOWN_TIMEOUT", "10"))

    with tempfile.TemporaryDirectory(prefix="solin-e2e-") as temp_dir:
        temp_root = Path(temp_dir)
        env = os.environ.copy()
        env["APPDATA"] = str(temp_root / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(temp_root / "AppData" / "Local")
        env.setdefault(
            "QT_LOGGING_RULES",
            "qt.qpa.mime=false;qt.multimedia.ffmpeg=false",
        )

        process = subprocess.Popen(  # noqa: S603 - e2e runs a user-supplied app artifact
            [str(exe_path), "--create-profile"],
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
