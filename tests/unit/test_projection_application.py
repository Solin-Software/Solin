from __future__ import annotations

from solin.core.projection.application import ProjectionSession
from solin.core.projection.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER


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


def test_projection_session_publishes_versioned_snapshots() -> None:
    session = ProjectionSession()
    revisions: list[int] = []
    unsubscribe = session.subscribe(lambda: revisions.append(session.revision))

    session.set_state({"type": "video", "title": "Welcome"})
    session.update_state(title="Updated")
    session.update_state(title="Updated")
    unsubscribe()
    session.reset_state()

    assert revisions == [1, 2]
    assert session.revision == 3


def test_projection_session_notifies_observers_in_subscription_order() -> None:
    session = ProjectionSession()
    notifications: list[str] = []

    session.subscribe(lambda: notifications.append("content"))
    session.subscribe(lambda: notifications.append("scenes"))
    session.subscribe(lambda: notifications.append("surfaces"))

    session.set_state({"type": "image"})

    assert notifications == ["content", "scenes", "surfaces"]


def test_projection_session_identity_excludes_incremental_state_updates() -> None:
    session = ProjectionSession()

    session.set_state({"type": "timer", "remaining": 10})
    active_session_id = session.session_id
    session.update_state(remaining=9)

    assert session.session_id == active_session_id
    session.reset_state()
    assert session.session_id == active_session_id + 1


def test_audio_only_playback_preserves_the_idle_presentation_identity() -> None:
    session = ProjectionSession()
    idle_presentation_id = session.presentation_session_id

    session.set_state({"type": "video", "is_audio": True, "title": "Song"})

    assert session.session_id == 1
    assert session.presentation_session_id == idle_presentation_id

    session.reset_state()

    assert session.session_id == 2
    assert session.presentation_session_id == idle_presentation_id


def test_audio_after_visual_content_creates_one_idle_presentation_identity() -> None:
    session = ProjectionSession()
    session.set_state({"type": "image", "data": b"image"})
    visual_presentation_id = session.presentation_session_id

    session.set_state({"type": "video", "is_audio": True, "title": "Song"})
    idle_presentation_id = session.presentation_session_id

    assert idle_presentation_id > visual_presentation_id

    session.reset_state()

    assert session.presentation_session_id == idle_presentation_id


def test_incremental_audio_flag_change_updates_only_visual_identity() -> None:
    session = ProjectionSession()
    session.set_state({"type": "video", "is_audio": False})
    playback_session_id = session.session_id
    visual_presentation_id = session.presentation_session_id

    session.update_state(is_audio=True)

    assert session.session_id == playback_session_id
    assert session.presentation_session_id > visual_presentation_id


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


def test_idle_choice_has_separate_revision_without_replacing_presentation():
    session = ProjectionSession()
    session.set_state({"type": "video", "title": "Playing", "is_audio": False})
    identity = (session.session_id, session.presentation_session_id)
    state = dict(session.state)
    published = []
    session.subscribe(lambda: published.append((session.revision, session.idle_media_revision)))

    session.set_idle_media_path("idle.mp4")
    session.set_idle_media_path("idle.mp4")
    session.set_idle_media_path("")

    assert published == [(2, 1), (3, 2)]
    assert (session.session_id, session.presentation_session_id) == identity
    assert session.state == state
    assert session.idle_media_path == ""


def test_idle_choice_notifies_pipeline_in_order():
    session = ProjectionSession()
    published = []
    session.subscribe(lambda: published.append(("content", session.idle_media_path)))
    session.subscribe(lambda: published.append(("scenes", session.idle_media_path)))
    session.subscribe(lambda: published.append(("surfaces", session.idle_media_path)))
    session.set_idle_media_path("idle.png")
    assert published == [("content", "idle.png"), ("scenes", "idle.png"), ("surfaces", "idle.png")]
