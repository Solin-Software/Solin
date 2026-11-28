from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from scripts.package_libobs_runtime import verify_packaged_sidecar
from tests.e2e._http_runtime import assert_packaged_http_runtime
from tests.e2e._media_runtime import assert_packaged_media_runtime
from tests.e2e._packaged_app import (
    assert_process_survives_startup,
    cleanup_solin_test_registry,
    isolated_app_env,
    path_from_env,
    run_packaged_headless,
    skip_if_solin_registry_exists,
)


pytestmark = pytest.mark.e2e


def test_packaged_qml_runtime() -> None:
    executable = path_from_env(
        "SOLIN_PACKAGED_EXE", purpose="packaged QML runtime qualification",
    ).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="solin-qml-e2e-") as directory:
        env = isolated_app_env(Path(directory))
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        process = run_packaged_headless(
            executable, ["--verify-qml-runtime"], env=env, timeout=30, purpose="QML",
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


def test_packaged_media_runtime() -> None:
    executable = path_from_env(
        "SOLIN_PACKAGED_EXE", purpose="packaged Python media client qualification",
    )
    assert_packaged_media_runtime(executable)


def test_packaged_http_runtime() -> None:
    executable = path_from_env(
        "SOLIN_PACKAGED_EXE", purpose="packaged HTTP runtime qualification",
    )
    assert_packaged_http_runtime(executable)


def test_packaged_sidecar_initializes_native_runtime_and_required_sources() -> None:
    executable = path_from_env(
        "SOLIN_PACKAGED_EXE", purpose="packaged libobs runtime smoke tests",
    )
    verify_packaged_sidecar(executable)


def test_packaged_app_survives_startup_window() -> None:
    skip_if_solin_registry_exists()
    exe_path = path_from_env(
        "SOLIN_PACKAGED_EXE",
        purpose="packaged application smoke tests",
    )
    try:
        with tempfile.TemporaryDirectory(prefix="solin-e2e-") as temp_dir:
            assert_process_survives_startup(
                exe_path,
                args=("--create-profile",),
                env=isolated_app_env(temp_root=Path(temp_dir)),
            )
    finally:
        cleanup_solin_test_registry()
