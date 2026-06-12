from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_wifi_receive_button_styles_parse_without_qt_warnings():
    repo_root = Path(__file__).resolve().parents[1]
    script = """
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication

messages = []
qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
app = QApplication([])

from app.core.i18n.manager import LanguageManager
from app.widgets.wifi_receive_widget import WifiReceiveWidget

widget = WifiReceiveWidget(LanguageManager())
widget.show()
app.processEvents()
stylesheet_errors = [
    message for message in messages if "parse stylesheet" in message.lower()
]
widget.close()
app.processEvents()
if stylesheet_errors:
    raise SystemExit("\\n".join(stylesheet_errors))
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
