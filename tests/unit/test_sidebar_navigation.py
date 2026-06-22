from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from solin.styles.icons import ICON_NAV_MEETINGS
from solin.widgets.common.collapsible_sidebar import (
    CollapsibleSidebarFrame,
    SIDEBAR_COLLAPSED_WIDTH,
    SIDEBAR_EXPANDED_WIDTH,
    SidebarChromeController,
)
from solin.widgets.common.sidebar_button import SidebarButton


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _SidebarSettingsStub:
    def __init__(self, collapsed: bool = False) -> None:
        self.collapsed = collapsed
        self.saved: list[bool] = []

    def sidebar_collapsed(self, _default: bool = False) -> bool:
        return self.collapsed

    def save_sidebar_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.saved.append(collapsed)


def test_sidebar_button_compact_mode_keeps_label_as_tooltip() -> None:
    _app()

    button = SidebarButton(ICON_NAV_MEETINGS, "Meetings")

    assert button.text() == "  Meetings"
    assert button.toolTip() == "Meetings"

    button.set_compact(True)

    assert button.text() == ""
    assert button.toolTip() == "Meetings"

    button.set_label("Meeting")

    assert button.text() == ""
    assert button.toolTip() == "Meeting"

    button.set_compact(False)

    assert button.text() == "  Meeting"


def test_sidebar_chrome_applies_collapsed_and_expanded_states() -> None:
    _app()
    frame = CollapsibleSidebarFrame()
    title = QLabel("Solin")
    subtitle = QLabel("Audio & Video")
    nav_button = SidebarButton(ICON_NAV_MEETINGS, "Meetings")
    toggle = QPushButton()
    settings = _SidebarSettingsStub()
    controller = SidebarChromeController(
        frame=frame,
        title_label=title,
        subtitle_label=subtitle,
        nav_buttons=[nav_button],
        toggle_button=toggle,
        settings=settings,
        collapse_tooltip="Collapse sidebar",
        expand_tooltip="Expand sidebar",
    )

    controller.set_collapsed(True, animate=False, persist=True)

    assert controller.is_collapsed() is True
    assert settings.saved == [True]
    assert frame.minimumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert frame.maximumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert title.isHidden()
    assert subtitle.isHidden()
    assert nav_button.text() == ""
    assert nav_button.toolTip() == "Meetings"
    assert toggle.toolTip() == "Expand sidebar"

    controller.set_collapsed(False, animate=False, persist=False)

    assert controller.is_collapsed() is False
    assert frame.minimumWidth() == SIDEBAR_EXPANDED_WIDTH
    assert frame.maximumWidth() == SIDEBAR_EXPANDED_WIDTH
    assert not title.isHidden()
    assert not subtitle.isHidden()
    assert nav_button.text() == "  Meetings"
    assert toggle.toolTip() == "Collapse sidebar"


def test_sidebar_chrome_reads_initial_collapsed_state() -> None:
    _app()
    frame = CollapsibleSidebarFrame()
    title = QLabel("Solin")
    subtitle = QLabel("Audio & Video")
    nav_button = SidebarButton(ICON_NAV_MEETINGS, "Meetings")
    controller = SidebarChromeController(
        frame=frame,
        title_label=title,
        subtitle_label=subtitle,
        nav_buttons=[nav_button],
        toggle_button=QPushButton(),
        settings=_SidebarSettingsStub(collapsed=True),
        collapse_tooltip="Collapse sidebar",
        expand_tooltip="Expand sidebar",
    )

    assert controller.is_collapsed() is True
    assert frame.minimumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert nav_button.text() == ""
