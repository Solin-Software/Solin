from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


_COMPILE_TIMEOUT_SECONDS = 30


def compile_inno_probe(script: Path) -> Path:
    """Compile a private installer probe without waiting on inherited output pipes."""
    if os.name != "nt":
        pytest.skip("The Inno installer contract requires Windows.")
    compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("The Inno installer contract requires Inno Setup 6.")

    # ISCC or its child processes may retain inherited stdout/stderr handles.
    # A pipe can therefore outlive the compiler and stall subprocess.run's
    # communicate() even after the compiler itself has finished.
    log = script.with_suffix(".compile.log")
    try:
        with log.open("wb") as output:
            result = subprocess.run(  # noqa: S603 - trusted installed Inno compiler
                [str(compiler), "/Q", str(script)],
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=_COMPILE_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Inno Setup compilation exceeded {_COMPILE_TIMEOUT_SECONDS}s for {script}:\n"
            f"{log.read_text(encoding='utf-8', errors='replace')}"
        ) from exc

    assert result.returncode == 0, log.read_text(encoding="utf-8", errors="replace")
    return script.parent / "probe.exe"
