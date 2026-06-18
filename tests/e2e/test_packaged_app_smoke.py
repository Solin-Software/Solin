from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.e2e._packaged_app import (
    assert_process_survives_startup,
    cleanup_solin_test_registry,
    isolated_app_env,
    path_from_env,
    skip_if_solin_registry_exists,
)


pytestmark = pytest.mark.e2e


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
