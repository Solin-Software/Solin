"""Register build adapters; project metadata lives in pyproject.toml."""

from pathlib import Path
import sys

from setuptools import setup

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.setuptools_commands import BuildPy  # noqa: E402

setup(cmdclass={"build_py": BuildPy})
