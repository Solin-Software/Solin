from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from solin.styles.icons import ICON_NAV_MEETINGS
from solin.widgets.common.collapsible_sidebar import (
    CollapsibleSidebarFrame,
    SIDEBAR_COLLAPSED_WIDTH,
    SIDEBAR_EXPANDED_WIDTH,
    SidebarChromeController,
)
from solin.widgets.common.profile_avatar_button import ProfileAvatarButton
from solin.widgets.common.sidebar_button import SidebarButton


class _SidebarSettingsStub:
    def __init__(self, collapsed: bool = False) -> None:
        self.collapsed = collapsed
        self.saved: list[bool] = []

    def sidebar_collapsed(self, _default: bool = False) -> bool:
        return self.collapsed

    def save_sidebar_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.saved.append(collapsed)


@pytest.fixture
def sidebar_factory(request):
    def create(*, collapsed=False, width=900):
        window = QWidget()
        request.addfinalizer(window.deleteLater)
        request.addfinalizer(window.close)
        window.resize(width, 700)
        layout = QHBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        frame = CollapsibleSidebarFrame(window)
        layout.addWidget(frame)
        layout.addWidget(QWidget(window), stretch=1)
        sidebar_layout = QVBoxLayout(frame)
        title = QLabel("Solin")
        subtitle = QLabel("Audio & Video")
        avatar = ProfileAvatarButton("Main Hall", profile_id="main_hall")
        nav_button = SidebarButton(ICON_NAV_MEETINGS, "Meetings")
        toggle = QPushButton()
        for widget in (title, subtitle, avatar, nav_button, toggle):
            sidebar_layout.addWidget(widget)
        settings = _SidebarSettingsStub(collapsed)
        controller = SidebarChromeController(
            frame=frame,
            width_source=window,
            title_label=title,
            subtitle_label=subtitle,
            profile_avatar_button=avatar,
            nav_buttons=[nav_button],
            toggle_button=toggle,
            settings=settings,
            collapse_tooltip="Collapse sidebar",
            expand_tooltip="Expand sidebar",
            parent=frame,
        )
        request.addfinalizer(controller._animation.stop)
        window.show()
        QApplication.processEvents()
        return SimpleNamespace(
            window=window, frame=frame, title=title, subtitle=subtitle,
            avatar=avatar, nav_button=nav_button, toggle=toggle,
            settings=settings, controller=controller,
        )

    return create


def test_sidebar_button_compact_mode_keeps_label_as_tooltip() -> None:
    button = SidebarButton(ICON_NAV_MEETINGS, "Meetings")
    try:
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
    finally:
        button.deleteLater()


def test_sidebar_chrome_applies_collapsed_and_expanded_states(sidebar_factory) -> None:
    sidebar = sidebar_factory()
    sidebar.controller.set_collapsed(True, animate=False, persist=True)
    assert sidebar.controller.is_collapsed()
    assert sidebar.settings.saved == [True]
    assert sidebar.frame.minimumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert sidebar.frame.maximumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert sidebar.title.isHidden()
    assert sidebar.subtitle.isHidden()
    assert sidebar.avatar.isHidden()
    assert sidebar.nav_button.text() == ""
    assert sidebar.nav_button.toolTip() == "Meetings"
    assert sidebar.toggle.toolTip() == "Expand sidebar"

    sidebar.controller.set_collapsed(False, animate=False, persist=False)
    assert not sidebar.controller.is_collapsed()
    assert sidebar.frame.minimumWidth() == SIDEBAR_EXPANDED_WIDTH
    assert sidebar.frame.maximumWidth() == SIDEBAR_EXPANDED_WIDTH
    assert not sidebar.title.isHidden()
    assert not sidebar.subtitle.isHidden()
    assert not sidebar.avatar.isHidden()
    assert sidebar.nav_button.text() == "  Meetings"
    assert sidebar.toggle.toolTip() == "Collapse sidebar"


def test_sidebar_chrome_reads_initial_collapsed_state(sidebar_factory) -> None:
    sidebar = sidebar_factory(collapsed=True)
    assert sidebar.controller.is_collapsed()
    assert sidebar.frame.minimumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert sidebar.avatar.isHidden()
    assert sidebar.nav_button.text() == ""


def test_sidebar_chrome_reveals_text_as_expansion_starts(sidebar_factory) -> None:
    sidebar = sidebar_factory(collapsed=True)
    sidebar.controller.set_collapsed(False, animate=True)
    assert not sidebar.title.isHidden()
    assert not sidebar.subtitle.isHidden()
    assert not sidebar.avatar.isHidden()
    assert sidebar.nav_button.text() == "  Meetings"


@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("width", [460, 559, 560, 561])
def test_sidebar_initial_state_respects_width_without_saving(sidebar_factory, collapsed, width):
    sidebar = sidebar_factory(collapsed=collapsed, width=width)
    assert sidebar.controller.is_collapsed() is (collapsed or width < 560)
    assert sidebar.toggle.isEnabled() is (width >= 560)
    assert sidebar.settings.collapsed is collapsed
    assert sidebar.settings.saved == []


@pytest.mark.parametrize("collapsed", [False, True])
def test_resize_restores_preference_without_persisting_layout(sidebar_factory, collapsed):
    sidebar = sidebar_factory(collapsed=collapsed)
    changes = []
    sidebar.controller.collapsed_changed.connect(changes.append)
    for width in (559, 460, 559, 560, 800, 559, 560):
        sidebar.window.resize(width, 700)
        expected_collapsed = collapsed or width < 560
        assert sidebar.controller.is_collapsed() is expected_collapsed
        assert sidebar.toggle.isEnabled() is (width >= 560)
        expected_width = SIDEBAR_COLLAPSED_WIDTH if expected_collapsed else SIDEBAR_EXPANDED_WIDTH
        assert sidebar.frame.width() == expected_width
        assert sidebar.settings.collapsed is collapsed
        assert sidebar.settings.saved == []
    assert changes == ([] if collapsed else [True, False, True, False])


@pytest.mark.parametrize("collapsed", [False, True])
def test_narrow_sidebar_rejects_toggle_without_changing_preference(sidebar_factory, collapsed):
    sidebar = sidebar_factory(collapsed=collapsed, width=460)
    sidebar.toggle.click()
    sidebar.controller.toggle()
    assert sidebar.controller.is_collapsed()
    assert sidebar.frame.width() == SIDEBAR_COLLAPSED_WIDTH
    assert sidebar.settings.collapsed is collapsed
    assert sidebar.settings.saved == []


def test_manual_preference_survives_narrow_and_wide_resizes(sidebar_factory):
    sidebar = sidebar_factory()
    for collapsed in (True, False):
        sidebar.toggle.click()
        assert sidebar.settings.collapsed is collapsed
        sidebar.window.resize(460, 700)
        assert sidebar.controller.is_collapsed()
        sidebar.window.resize(560, 700)
        assert sidebar.controller.is_collapsed() is collapsed
    assert sidebar.settings.saved == [True, False]


@pytest.mark.parametrize("collapsed", [False, True])
def test_narrow_resize_stops_animation_and_restores_preference(sidebar_factory, collapsed):
    sidebar = sidebar_factory(collapsed=collapsed)
    sidebar.toggle.click()
    assert sidebar.controller._animation.state() is QAbstractAnimation.State.Running
    sidebar.controller._animation.setCurrentTime(sidebar.controller._animation.duration() // 2)
    assert SIDEBAR_COLLAPSED_WIDTH < sidebar.frame.width() < SIDEBAR_EXPANDED_WIDTH
    assert sidebar.settings.saved == [not collapsed]
    sidebar.window.resize(559, 700)
    assert sidebar.controller._animation.state() is QAbstractAnimation.State.Stopped
    assert sidebar.controller.is_collapsed()
    assert sidebar.frame.minimumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert sidebar.frame.maximumWidth() == SIDEBAR_COLLAPSED_WIDTH
    assert not sidebar.toggle.isEnabled()
    sidebar.window.resize(560, 700)
    assert sidebar.controller.is_collapsed() is not collapsed
    assert sidebar.frame.width() == (SIDEBAR_EXPANDED_WIDTH if collapsed else SIDEBAR_COLLAPSED_WIDTH)
    assert sidebar.toggle.isEnabled()
    assert sidebar.settings.saved == [not collapsed]
