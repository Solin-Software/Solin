"""Exercise pytest isolation with real, overlapping child invocations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from PySide6.QtCore import QObject
import pytest

from tests._paths import REPO_ROOT


def test_progress_log_write_failure_does_not_skip_deferred_destruction(tmp_path, monkeypatch):
    from tests import conftest

    destroyed = []
    owned = QObject()
    owned.destroyed.connect(lambda: destroyed.append(True))
    item = SimpleNamespace(
        nodeid="diagnostic-write-failure",
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(getplugin=lambda _name: None),
        ),
    )
    # Opening a directory as the JSONL file fails on every supported platform.
    # Restore the destination before pytest's real protocol teardown runs.
    with monkeypatch.context() as context:
        context.setenv("SOLIN_TEST_PROGRESS_LOG", str(tmp_path))
        protocol = conftest.pytest_runtest_protocol(item, None)
        next(protocol)
        owned.deleteLater()
        with pytest.raises(OSError):
            protocol.send(None)
        assert destroyed == [True], "Diagnostics prevented Qt-owned destruction"


def test_concurrent_pytest_invocations_preserve_each_others_temporary_files(tmp_path):
    child = tmp_path / "test_child.py"
    ready = tmp_path / "ready.json"
    release = tmp_path / "release"
    child.write_text(
        '''import json
import os
from pathlib import Path
import time

def test_owned_temporary_file(tmp_path):
    owned = tmp_path / "owned.txt"
    owned.write_text(os.environ["ISOLATION_ROLE"], encoding="utf-8")
    ready = Path(os.environ["ISOLATION_READY"])
    release = Path(os.environ["ISOLATION_RELEASE"])
    if os.environ["ISOLATION_ROLE"] == "first":
        pending = ready.with_suffix(".pending")
        pending.write_text(json.dumps(str(owned)), encoding="utf-8")
        pending.replace(ready)
        deadline = time.monotonic() + 20
        while not release.exists():
            assert time.monotonic() < deadline, "Parent did not release the first invocation"
            time.sleep(0.01)
        assert owned.read_text(encoding="utf-8") == "first"
    else:
        other = Path(json.loads(ready.read_text(encoding="utf-8")))
        assert other != owned, "Invocations reused the same temporary directory"
        assert other.read_text(encoding="utf-8") == "first"
''',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(ISOLATION_READY=str(ready), ISOLATION_RELEASE=str(release))
    command = [
        sys.executable, "-m", "pytest", str(child), "--noconftest", "-q",
        "-c", str(REPO_ROOT / "pyproject.toml"),
    ]
    first = subprocess.Popen(
        command, cwd=tmp_path, env={**environment, "ISOLATION_ROLE": "first"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert first.poll() is None, "First invocation exited before acquiring its temp directory"
            assert time.monotonic() < deadline, "First invocation did not acquire its temp directory"
            time.sleep(0.01)
        owned = Path(json.loads(ready.read_text(encoding="utf-8")))
        assert owned.read_text(encoding="utf-8") == "first"
        second = subprocess.run(
            command, cwd=tmp_path, env={**environment, "ISOLATION_ROLE": "second"},
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert second.returncode == 0, second.stdout + second.stderr
    finally:
        release.touch()
        try:
            output, _ = first.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate()
            raise
    assert first.returncode == 0, output
