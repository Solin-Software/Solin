from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from solin.bootstrap.startup_shell import StartupShellWindow


_APP = QApplication.instance() or QApplication([])


class _CancellableLoad:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _ClosableWindow:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_startup_shell_presents_a_real_first_frame_and_honors_final_size() -> None:
    script = """
import json
import time
from PySide6.QtWidgets import QApplication
from solin.bootstrap.startup_shell import StartupShellWindow
app = QApplication([])
shell = StartupShellWindow(width=900, height=700)
presented = []
shell.first_frame_presented.connect(lambda: presented.append(True))
shell.show()
deadline = time.monotonic() + 1.0
while not presented and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.005)
print(json.dumps({"presented": presented, "size": [shell.width(), shell.height()]}))
shell.close()
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )

    assert json.loads(result.stdout) == {"presented": [True], "size": [900, 700]}


def test_closing_startup_shell_cancels_pending_load() -> None:
    shell = StartupShellWindow(width=900, height=700)
    load = _CancellableLoad()
    target = _ClosableWindow()
    shell.set_load_handle(load)
    shell.set_target_window(target)

    shell.closeEvent(QCloseEvent())

    assert load.cancelled is True
    assert target.closed is True
