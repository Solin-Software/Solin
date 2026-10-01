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


@pytest.fixture(autouse=True)
def isolated_linked_sync_state(tmp_path, monkeypatch):
    """Never read/write the operator's durable sync history from test replicas."""
    from solin.core.ingest.sync import journal
    from solin.core.playlists import linked_folder

    linked_folder._SERVICES.clear()
    monkeypatch.setattr(journal, "_default_state_dir", lambda: tmp_path / "replica-state")
    yield
    linked_folder._SERVICES.clear()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            relative = Path(str(item.path)).resolve().relative_to(Path(__file__).resolve().parent)
        except ValueError:
            continue

        suite = relative.parts[0] if relative.parts else ""
        if suite in {"unit", "integration", "contract", "e2e"}:
            item.add_marker(getattr(pytest.mark, suite))
