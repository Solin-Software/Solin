"""Acceptance checks for Windows-specific packaged integrations."""
from __future__ import annotations

import json
import sys

import pytest

from tests.e2e._packaged_app import isolated_app_env, path_from_env, run_packaged_headless


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation runtime"),
]


def test_packaged_zoom_runtime(tmp_path) -> None:
    executable = path_from_env(
        "SOLIN_PACKAGED_EXE", purpose="packaged Zoom runtime qualification",
    )
    environment = isolated_app_env(tmp_path)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    process = run_packaged_headless(
        executable, ["--verify-zoom-runtime"], env=environment,
        timeout=30, purpose="Zoom",
    )
    diagnostics = process.stderr.decode("utf-8", errors="replace")[-16000:]
    assert process.returncode == 0, (
        f"Packaged Zoom qualification exited with {process.returncode}:\n"
        f"{process.stdout.decode('utf-8', errors='replace')}\n{diagnostics}"
    )
    assert json.loads(process.stdout) == {"schema": "solin.zoom-runtime.v1", "ok": True}
