from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Establish the most capable Qt application before test modules are imported.
# A QCoreApplication created by a model-only module cannot later be upgraded to
# QApplication, which previously caused the complete QML host module to skip.
from PySide6.QtCore import (  # noqa: E402
    QCoreApplication, QEvent, QObject, QtMsgType, qFormatLogMessage, qInstallMessageHandler,
)
from PySide6.QtWidgets import QApplication  # noqa: E402


_QT_APPLICATION = QApplication.instance() or QApplication([])


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.failed:
        # A later native crash must not erase earlier assertion diagnostics by
        # preventing pytest from reaching its terminal summary.
        sys.stderr.write(f"\n{report.nodeid} [{report.when}]\n{report.longreprtext}\n")
        sys.stderr.flush()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    pool = nullcontext()
    if sys.platform == "darwin" and _QT_APPLICATION.platformName() == "cocoa":
        import objc

        # Hosts are created outside Cocoa's event dispatcher. Its local pools
        # cannot drain objects already autoreleased into an outer pool.
        pool = objc.autorelease_pool()
    with pool:
        try:
            return (yield)
        finally:
            # processEvents() does not finish DeferredDelete outside an event
            # loop. Complete finalizers' scheduled destruction in this protocol,
            # before draining its native pool or starting another test.
            capture = item.config.pluginmanager.getplugin("capturemanager")
            diagnostics = (
                capture.global_and_fixture_disabled() if capture is not None else nullcontext()
            )
            # Fatal Qt destruction messages cannot wait for pytest's summary:
            # abort() would discard its captured output along with the process.
            with diagnostics:
                previous = None

                def report_destruction(mode, context, message):
                    if previous is None or mode == QtMsgType.QtFatalMsg:
                        print(qFormatLogMessage(mode, context, message), file=sys.stderr, flush=True)
                    if previous is not None:
                        previous(mode, context, message)

                previous = qInstallMessageHandler(report_destruction)
                try:
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                finally:
                    qInstallMessageHandler(previous)


@pytest.fixture(autouse=True)
def isolated_linked_sync_state(tmp_path, monkeypatch):
    """Never read/write the operator's durable sync history from test replicas."""
    from solin.core.ingest.sync import journal
    from solin.core.playlists import linked_folder

    linked_folder._SERVICES.clear()
    monkeypatch.setattr(journal, "_default_state_dir", lambda: tmp_path / "replica-state")
    yield
    linked_folder._SERVICES.clear()


@pytest.fixture
def qt_object_owner() -> Iterator[QObject]:
    """Keep QObject children owned until destruction on the Qt thread."""
    owner = QObject(_QT_APPLICATION)
    try:
        yield owner
    finally:
        # The protocol hook completes DeferredDelete after all finalizers.
        # Python cyclic GC must not choose a worker thread to destroy children.
        owner.deleteLater()


@pytest.fixture
def scene_workspace_factory(request, tmp_path):
    """Own workspace writers through teardown and verify that persistence stops."""
    from solin.core.scenes.workspace import SceneWorkspaceService

    def create(*args, **kwargs) -> SceneWorkspaceService:
        workspace = SceneWorkspaceService(*args, **kwargs)

        def verify_persistence() -> None:
            queue = workspace._runtime_persistence
            # Controller.close() already closes its workspace. Inspect the owned
            # queue rather than invoking that shutdown path a second time.
            with queue._condition:
                closing = queue._closing
            if not closing:
                queue.close()
            # Closing is irreversible, but an in-flight storage call may
            # finish after the controller's deadline. Join only this writer.
            if queue._thread.is_alive():
                queue._thread.join(timeout=2.0)
            flushed = queue.flush(timeout_seconds=0)
            stopped = not queue._thread.is_alive()
            assert flushed and stopped, (
                f"Scene workspace persistence cleanup failed: flushed={flushed}, "
                f"writer_stopped={stopped}, last_error={queue.last_error!r}"
            )

        def close_workspace() -> None:
            queue = workspace._runtime_persistence
            with queue._condition:
                closing = queue._closing
            if not closing:
                workspace.close()

        # Separate finalizers let pytest verify/drain this writer and clean up
        # other workspaces even when one workspace's close raises.
        request.addfinalizer(verify_persistence)
        request.addfinalizer(close_workspace)
        return workspace

    return create


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            relative = Path(str(item.path)).resolve().relative_to(Path(__file__).resolve().parent)
        except ValueError:
            continue

        suite = relative.parts[0] if relative.parts else ""
        if suite in {"unit", "integration", "contract", "e2e"}:
            item.add_marker(getattr(pytest.mark, suite))
