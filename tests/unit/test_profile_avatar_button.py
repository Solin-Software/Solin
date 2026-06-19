from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from solin.ui.helpers import avatar_colors, initials
from solin.widgets.common.profile_avatar_button import ProfileAvatarButton


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_profile_avatar_uses_id_for_color_and_name_for_initials() -> None:
    _app()

    avatar = ProfileAvatarButton("Renamed Hall", profile_id="main_hall")

    assert avatar._colors() == avatar_colors("main_hall")
    assert avatar._initials() == initials("Renamed Hall")

