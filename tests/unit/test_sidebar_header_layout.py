from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from solin.controllers.main_window_ui_controller import MainWindowUiController
from solin.widgets.common.profile_avatar_button import ProfileAvatarButton


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_sidebar_header_places_avatar_before_title_column() -> None:
    _app()

    avatar = ProfileAvatarButton("Main Hall", profile_id="main_hall")
    layout = MainWindowUiController._build_sidebar_header_layout(
        QLabel("Solin"),
        QLabel("Main Hall"),
        avatar,
    )

    assert layout.itemAt(0).widget() is avatar
    assert layout.itemAt(1).layout() is not None
