from __future__ import annotations

import gc
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


# Reclaim PySide ``QObject`` cycles periodically so they cannot pile up across
# the suite. PySide6 objects routinely sit in reference cycles (signal/slot
# connections, parent/child links, closures), so plain refcounting never frees
# them — they wait for the cyclic collector, which the test process seldom
# triggers on its own. Under the ``offscreen`` platform those still-live C++
# objects (and the native resources they hold, e.g. Qt Multimedia pipelines)
# accumulate until constructing a fresh ``QVideoWidget`` faults in the native
# layer — a load-dependent segfault whose crash site drifts between widget
# tests. The crash needs roughly ~130 accumulated tests, so collecting every
# _GC_EVERY tests keeps the population far below that at a fraction of the cost
# of collecting after every test. Headless-test hygiene only; the app runs a
# real event loop that drains deferred deletions continuously.
_GC_EVERY = 20
_tests_since_gc = 0


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    global _tests_since_gc
    _tests_since_gc += 1
    if nextitem is None or _tests_since_gc >= _GC_EVERY:
        _tests_since_gc = 0
        gc.collect()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            relative = Path(str(item.path)).resolve().relative_to(Path(__file__).resolve().parent)
        except ValueError:
            continue

        suite = relative.parts[0] if relative.parts else ""
        if suite in {"unit", "integration", "contract", "e2e"}:
            item.add_marker(getattr(pytest.mark, suite))
