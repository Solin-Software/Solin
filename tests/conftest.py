from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Establish the most capable Qt application before test modules are imported.
# A QCoreApplication created by a model-only module cannot later be upgraded to
# QApplication, which previously caused the complete QML host module to skip.
from PySide6.QtWidgets import QApplication  # noqa: E402


_QT_APPLICATION = QApplication.instance() or QApplication([])


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            relative = Path(str(item.path)).resolve().relative_to(Path(__file__).resolve().parent)
        except ValueError:
            continue

        suite = relative.parts[0] if relative.parts else ""
        if suite in {"unit", "integration", "contract", "e2e"}:
            item.add_marker(getattr(pytest.mark, suite))
