"""Qualify media extraction through the Python client inside an external artifact."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from solin.bootstrap.media_runtime_verification import MEDIA_RUNTIME_CHECKS
from tests.e2e._packaged_app import isolated_app_env, run_packaged_headless

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "media_extract"


def assert_packaged_media_runtime(
    exe_path: Path,
    *,
    fixtures_directory: Path = _FIXTURES,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> None:
    """Require the delivered client to locate its helper and decode fixed fixtures."""
    executable = exe_path.resolve(strict=True)
    fixtures = fixtures_directory.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sln-media-") as directory:
        isolated = isolated_app_env(Path(directory))
        environment = dict(isolated)
        if env is not None:
            environment.update(env)
        for name in (
            "APPDATA",
            "LOCALAPPDATA",
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "SOLIN_IPC_SERVER_NAME",
            "TMPDIR",
            "TMP",
            "TEMP",
        ):
            if name in isolated:
                environment[name] = isolated[name]
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "DYLD_LIBRARY_PATH",
            "DYLD_FALLBACK_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "SOLIN_MEDIA_EXTRACT_FFMPEG_DIR",
            "LIBOBS_PATH",
            "OBS_DATA_PATH",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "APPDIR",
        ):
            environment.pop(name, None)
        environment["PATH"] = ""
        environment["QT_QPA_PLATFORM"] = "offscreen"
        process = run_packaged_headless(
            executable,
            ["--verify-media-runtime", str(fixtures)],
            env=environment,
            timeout=timeout,
            purpose="media",
        )
        diagnostics = process.stderr.decode("utf-8", errors="replace")[-16000:]
        assert process.returncode == 0, (
            f"Packaged media qualification exited with {process.returncode}:\n"
            f"{process.stdout.decode('utf-8', errors='replace')}\n{diagnostics}"
        )
        try:
            result = json.loads(process.stdout)
        except (ValueError, UnicodeError) as error:
            pytest.fail(f"Invalid packaged media qualification JSON: {error}\n{diagnostics}")
        expected = {
            "schema": "solin.media-runtime.v1",
            "ok": True,
            "checks": list(MEDIA_RUNTIME_CHECKS),
        }
        assert result == expected and result["ok"] is True, (
            f"Unexpected packaged media qualification result: {result!r}\n{diagnostics}"
        )
        for name in (
            "APPDATA",
            "LOCALAPPDATA",
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            assert not any(Path(environment[name]).iterdir()), (
                f"Headless media qualification created application state in {name}"
            )
