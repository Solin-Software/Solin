"""QML qualification shared by standalone and installer-upgrade smokes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.e2e._packaged_app import isolated_app_env, run_packaged_headless


def assert_packaged_qml_runtime(exe_path: Path, *, env: dict[str, str] | None = None) -> None:
    executable = exe_path.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="solin-qml-e2e-") as directory:
        environment = dict(env) if env is not None else isolated_app_env(Path(directory))
        for name in ("PYTHONPATH", "PYTHONHOME"):
            environment.pop(name, None)
        process = run_packaged_headless(
            executable, ["--verify-qml-runtime"], env=environment, timeout=30, purpose="QML",
        )
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")[-16000:]
    assert process.returncode == 0, (
        f"Packaged QML qualification exited with {process.returncode}:\n{stdout}\n{stderr}"
    )
    try:
        result = json.loads(stdout)
    except ValueError as error:
        pytest.fail(f"Invalid packaged QML qualification JSON: {error}\n{stdout}\n{stderr}")
    assert isinstance(result, dict) and result.get("schema") == "solin.qml-runtime.v1" and result.get("ok") is True, (
        f"Unexpected packaged QML qualification result: {result!r}\n{stderr}"
    )
    sources = result.get("sources")
    assert isinstance(sources, dict), f"Missing packaged QML sources: {result!r}\n{stderr}"
    for name, module in (("QuickAccessToolbar", "Solin/Shell"), ("TimerToggle", "Solin")):
        expected = f"qrc:/qt/qml/{module}/{name}.qml"
        assert sources.get(name) == expected, (
            f"{name} did not load from {expected}: {result!r}\n{stderr}"
        )
