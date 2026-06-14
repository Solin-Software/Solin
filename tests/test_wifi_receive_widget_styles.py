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
from pathlib import Path

messages = []
qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
app = QApplication([])

from solin.core.i18n.manager import LanguageManager
from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from solin.core.foundation.settings_store import GlobalSettingsStore
from solin.ui.media_info import MediaInfoQueue, MediaInfoService
from solin.core.media.profile_store import ProfileMediaStore
from solin.core.ingest.wifi_server import WifiReceiveServer
from solin.widgets.wifi_receive_widget import WifiReceiveWidget

class _Notifications:
    def success(self, *_args):
        pass
    def information(self, *_args):
        pass
    def warning(self, *_args):
        pass
    def error(self, *_args):
        pass

runtime_paths = RuntimePaths.from_roots(
    data_dir=Path("data"),
    cache_dir=Path("cache"),
)
profile_paths = ProfilePaths.from_roots(
    data_dir=Path("data"),
    cache_dir=Path("cache"),
    profile_id="test",
)
widget = WifiReceiveWidget(
    LanguageManager(
        global_settings=GlobalSettingsStore.create(),
        jw_languages_cache_file=Path("jw_languages.json"),
    ),
    notifications=_Notifications(),
    profile_paths=profile_paths,
    runtime_paths=runtime_paths,
    profile_media_store=ProfileMediaStore(
        profile_paths.embedded_dir,
        profile_paths.images_dir,
    ),
    wifi_receive_server_factory=lambda parent: WifiReceiveServer(
        embedded_dir=profile_paths.embedded_dir,
        parent=parent,
    ),
    media_info_service_factory=lambda parent: MediaInfoService(
        lambda owner: MediaInfoQueue(
            runtime_paths.media_cache_dir,
            runtime_paths.thumb_cache_dir,
            owner,
        ),
        parent,
    ),
)
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
    env["PYTHONPATH"] = str(repo_root / "src")

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
