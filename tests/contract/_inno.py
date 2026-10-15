from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e._windows_process import run_windows_process_tree


_COMPILE_TIMEOUT_SECONDS = 30


def compile_inno_probe(script: Path) -> Path:
    """Compile a private probe and drain its owned compiler process tree."""
    if os.name != "nt":
        pytest.skip("The Inno installer contract requires Windows.")
    compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("The Inno installer contract requires Inno Setup 6.")

    # The shared runner captures through files and owns descendants, so compiler
    # timeout cannot leave helpers running or wait on inherited output pipes.
    try:
        result = run_windows_process_tree(
            [str(compiler), str(script)], timeout=_COMPILE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Inno Setup compilation exceeded {_COMPILE_TIMEOUT_SECONDS}s for {script}:\n"
            f"{exc.stdout or ''}{exc.stderr or ''}"
        ) from exc

    assert result.returncode == 0, result.stdout + result.stderr
    return script.parent / "probe.exe"
