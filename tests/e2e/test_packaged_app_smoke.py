from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.package_libobs_runtime import verify_packaged_sidecar
from tests.e2e._http_runtime import assert_packaged_http_runtime
from tests.e2e._packaged_app import (
    assert_process_survives_startup,
    cleanup_solin_test_registry,
    isolated_app_env,
    path_from_env,
    skip_if_solin_registry_exists,
)


pytestmark = pytest.mark.e2e


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
