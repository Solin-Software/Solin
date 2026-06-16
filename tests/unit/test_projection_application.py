from __future__ import annotations

from solin.core.projection.application import ProjectionSession
from solin.core.ui.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER


class _Screen:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Allocation:
    def __init__(self) -> None:
        self.owners = {}
        self.confirmed = []

    def owner_of(self, screen) -> str:
        return self.owners.get(screen.name(), OWNER_MEDIA)

    def set_owner(self, screen, owner: str) -> None:
        self.owners[screen.name()] = owner

    def confirm_assignment(self, screen, new_owner: str) -> None:
        self.confirmed.append((screen.name(), new_owner))
        self.set_owner(screen, new_owner)


def test_projection_session_owns_state_windows_and_idle_media() -> None:
    session = ProjectionSession()
    secondary = object()
    floating = object()

    session.projection_windows = [secondary]
    session.floating_preview_window = floating
    session.set_idle_media_path("idle.mp4")
    session.set_tab_projection_active(True)
    session.set_state({"type": "video", "is_audio": False})

    assert session.state == {"type": "video", "is_audio": False}
    assert session.state_type == "video"
    assert session.idle_media_path == "idle.mp4"
    assert session.tab_projection_active is True
    assert session.all_windows() == [secondary, floating]

    session.reset_state()
    assert session.state == {"type": "idle"}


def test_projection_session_fallback_media_visibility_without_allocation() -> None:
    screen = _Screen("DISPLAY2")
    session = ProjectionSession(media_hidden_screen_names={"DISPLAY2"})

    assert not session.media_eligible(screen)

    session.show_media_on_screen_name("DISPLAY2")
    assert session.media_eligible(screen)

    session.hide_media_on_screen_name("DISPLAY2")
    assert session.media_hidden_screen_names == {"DISPLAY2"}

    session.clear_hidden_media_screens()
    assert session.media_hidden_screen_names == set()


def test_projection_session_delegates_monitor_ownership_to_allocation() -> None:
    screen = _Screen("DISPLAY2")
    allocation = _Allocation()
    session = ProjectionSession(allocation=allocation)

    assert session.media_eligible(screen)
    assert not session.timer_reserved(screen)

    session.set_media_owner(screen, active=False)
    assert allocation.owners == {"DISPLAY2": OWNER_OFF}
    assert not session.media_eligible(screen)

    session.set_media_owner(screen, active=True)
    assert allocation.owners == {"DISPLAY2": OWNER_MEDIA}

    allocation.owners["DISPLAY2"] = OWNER_TIMER
    session.set_media_owner(screen, active=True)
    assert allocation.owners["DISPLAY2"] == OWNER_TIMER
    assert session.timer_reserved(screen)

    session.confirm_media_assignment(screen)
    assert allocation.confirmed == [("DISPLAY2", OWNER_MEDIA)]
    assert allocation.owners["DISPLAY2"] == OWNER_MEDIA
