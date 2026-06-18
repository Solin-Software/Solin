from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Could not locate repository root from tests package.")


REPO_ROOT = _repo_root()
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
