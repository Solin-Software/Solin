from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests._paths import REPO_ROOT
from tests.e2e._media_runtime import assert_packaged_media_runtime


def test_external_media_role_runs_the_delivered_client_without_qt_or_host_path(
    tmp_path, monkeypatch
):
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen
    launches = []

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        code = (
            "import runpy, sys; "
            "sys.modules['PySide6'] = None; "
            "sys.modules['solin.bootstrap.application'] = None; "
            f"runpy.run_path({str(REPO_ROOT / 'main.py')!r}, run_name='__main__')"
        )
        return original_popen([sys.executable, "-c", code, *command[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    environment = {
        "PATH": "external-tools",
        "PYTHONPATH": "host-packages",
        "PYTHONHOME": "host-python",
        "LD_LIBRARY_PATH": "SDK",
        "LD_PRELOAD": "SDK",
        "DYLD_LIBRARY_PATH": "SDK",
        "SOLIN_MEDIA_EXTRACT_FFMPEG_DIR": "SDK",
        "LIBOBS_PATH": "host-OBS",
        "DISPLAY": ":999",
        "WAYLAND_DISPLAY": "host-display",
        "HOME": str(tmp_path / "operator-home"),
    }
    assert_packaged_media_runtime(executable, env=environment)
    command, options = launches[0]
    assert command == [
        str(executable.resolve()),
        "--verify-media-runtime",
        str(REPO_ROOT / "tests/fixtures/media_extract"),
    ]
    assert options["env"]["PATH"] == ""
    assert options["env"]["HOME"] != environment["HOME"]
    assert not any(name in options["env"] for name in environment if name not in {"PATH", "HOME"})
    assert environment["PATH"] == "external-tools"


@pytest.mark.parametrize(
    "report",
    [
        "not JSON",
        {"schema": "solin.media-runtime.v1", "ok": True, "checks": []},
        {"schema": "solin.media-runtime.v1", "ok": False, "error": "RuntimeError"},
    ],
)
def test_external_media_role_rejects_invalid_or_incomplete_reports(tmp_path, monkeypatch, report):
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen

    def launch(command, **kwargs):
        text = report if isinstance(report, str) else json.dumps(report)
        return original_popen([sys.executable, "-c", f"print({text!r})"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        assert_packaged_media_runtime(executable)


def test_external_media_role_timeout_reaps_the_owned_process_and_closes_pipes(
    tmp_path, monkeypatch
):
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen
    processes = []

    def launch(command, **kwargs):
        process = original_popen(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(pytest.fail.Exception, match="exceeded"):
        assert_packaged_media_runtime(executable, timeout=0.2)
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed
    assert processes[0].stderr.closed


@pytest.mark.skipif(os.name != "posix", reason="AppImage shell launchers run on POSIX")
def test_linux_payload_launcher_resolves_itself_with_empty_path(tmp_path):
    recipe = (REPO_ROOT / "scripts/build_solin.sh").read_text()
    script = recipe.split("<<'LAUNCHER'\n", 1)[1].split("\nLAUNCHER", 1)[0]
    payload = tmp_path / "payload with spaces"
    payload.mkdir()
    launcher = payload / "run-solin"
    launcher.write_text(script)
    launcher.chmod(0o755)
    executable = payload / "Solin.bin"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
    executable.chmod(0o755)
    result = subprocess.run(
        [str(launcher), "payload-resolved"],
        cwd=tmp_path,
        env={**os.environ, "PATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "payload-resolved"
