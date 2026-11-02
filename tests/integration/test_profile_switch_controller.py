from types import SimpleNamespace

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from solin.bootstrap import profile_flow
from solin.controllers.profile_switch_controller import ProfileSwitchController
from solin.core.profiles.models import ProfileInfo


class _Window(QWidget):
    switch_profile_requested = Signal()
    profile_transition_available = True


class _Overlay(QWidget):
    cancelled = Signal()
    profile_selected = Signal(str)
    create_profile_requested = Signal()
    logout_requested = Signal()

    def __init__(self, parent, current_id, *, profiles):
        super().__init__(parent)
        self.current_id = current_id
        self.profiles = profiles


@pytest.fixture
def wired(monkeypatch):
    overlays = []
    window = _Window()
    try:
        current = ProfileInfo(id="current", name="Current")
        target = ProfileInfo(id="target", name="Target")
        profiles = SimpleNamespace(
            active_profile=current,
            profiles=[current, target],
            get_profile=lambda identifier: next(
                (profile for profile in (current, target) if profile.id == identifier),
                None,
            ),
        )

        def create_overlay(*args, **kwargs):
            overlay = _Overlay(*args, **kwargs)
            overlays.append(overlay)
            return overlay

        monkeypatch.setattr("solin.ui.profile_switch_overlay.ProfileSwitchOverlay", create_overlay)
        flow = profile_flow.wire_profile_switch(
            object(),
            [window],
            profiles,
            shutdown=lambda: None,
        )
        yield SimpleNamespace(window=window, profiles=profiles, flow=flow, overlays=overlays)
    finally:
        window.close()
        window.deleteLater()


def test_request_switch_emits_main_window_signal(wired):
    requested = []
    wired.window.switch_profile_requested.connect(lambda: requested.append(True))
    controller = ProfileSwitchController(wired.window.switch_profile_requested.emit)
    assert controller.request_switch() is True
    assert requested == [True]


def test_profile_switch_controller_uses_explicit_dependencies():
    controller = ProfileSwitchController(lambda: None)
    assert not hasattr(controller, "_window")


def test_profile_switch_is_blocked_before_opening_the_profile_overlay():
    requested = []
    blocked = []
    controller = ProfileSwitchController(
        lambda: requested.append(True),
        can_switch=lambda: False,
        notify_blocked=lambda: blocked.append(True),
    )
    assert controller.request_switch() is False
    assert requested == []
    assert blocked == [True]


def test_wire_profile_switch_opens_only_one_overlay(wired):
    wired.window.switch_profile_requested.emit()
    wired.window.switch_profile_requested.emit()
    assert len(wired.overlays) == 1
    overlay = wired.overlays[0]
    assert overlay.parent() is wired.window
    assert overlay.current_id == "current"
    assert overlay.profiles == wired.profiles.profiles


@pytest.mark.parametrize(
    "signal,method,args",
    [
        ("profile_selected", "select_profile", ("target",)),
        ("create_profile_requested", "create_profile", ()),
        ("logout_requested", "logout", ()),
    ],
)
def test_overlay_dispatches_to_shared_transition_controller(
    wired, monkeypatch, signal, method, args
):
    calls = []
    monkeypatch.setattr(wired.flow, method, lambda *arguments: calls.append(arguments))
    wired.window.switch_profile_requested.emit()
    getattr(wired.overlays[0], signal).emit(*args)
    assert calls == [args]


def test_cancelled_overlay_cannot_dispatch_stale_events(wired, monkeypatch):
    launches = []
    monkeypatch.setattr(wired.flow, "logout", lambda: launches.append(True))
    wired.window.switch_profile_requested.emit()
    overlay = wired.overlays[0]
    overlay.cancelled.emit()
    overlay.logout_requested.emit()
    assert wired.flow._overlay is None
    assert launches == []
    wired.window.switch_profile_requested.emit()
    assert len(wired.overlays) == 2


def test_current_profile_selection_dismisses_without_transition(wired):
    wired.window.switch_profile_requested.emit()
    wired.overlays[0].profile_selected.emit("current")
    assert wired.flow._overlay is None
    assert not wired.flow._transitioning
    assert wired.window.isEnabled()
