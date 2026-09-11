"""Distribution jobs must execute every selected acceptance test."""
from __future__ import annotations

import os
from collections.abc import Generator

import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport() -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if os.environ.get("SOLIN_E2E_STRICT") == "1" and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"Required release test skipped: {report.nodeid}: {report.longrepr}"
    return report
