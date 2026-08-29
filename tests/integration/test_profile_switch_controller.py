from solin.bootstrap import profile_flow
from solin.controllers.profile_switch_controller import ProfileSwitchController


class _SignalStub:
    def __init__(self):
        self.emitted = 0
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        self.emitted += 1
        for callback in list(self.callbacks):
            callback(*args)


class _Profile:
    def __init__(self, name):
        self.name = name


class _ProfileService:
    def __init__(self, active_profile=None):
        self.active_profile = active_profile
        self.profiles = [active_profile] if active_profile is not None else []


class _WindowStub:
    def __init__(self):
        self.switch_profile_requested = _SignalStub()
        self.removed_filters = []
        self.enabled = []

    def removeEventFilter(self, event_filter):
        self.removed_filters.append(event_filter)

    def setEnabled(self, enabled):
        self.enabled.append(enabled)


def test_request_switch_emits_main_window_signal():
    window = _WindowStub()
    controller = ProfileSwitchController(window.switch_profile_requested.emit)

    assert controller.request_switch() is True

    assert window.switch_profile_requested.emitted == 1


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


def test_wire_profile_switch_relaunches_selected_profile_from_overlay(monkeypatch):
    overlays = []
    relaunches = []

    class _Overlay:
        def __init__(self, parent, current_id, *, profiles):
            self.parent = parent
            self.current_id = current_id
            self.profiles = profiles
            self.cancelled = _SignalStub()
            self.profile_selected = _SignalStub()
            self.create_profile_requested = _SignalStub()
            self.shown = 0
            self.raised = 0
            self.deleted = 0
            overlays.append(self)

        def show(self):
            self.shown += 1

        def raise_(self):
            self.raised += 1

        def deleteLater(self):
            self.deleted += 1

    app = object()
    window = _WindowStub()
    manager = _ProfileService(_Profile("Current"))
    manager.active_profile.id = "profile-a"

    monkeypatch.setattr("solin.ui.profile_switch_overlay.ProfileSwitchOverlay", _Overlay)
    monkeypatch.setattr(
        profile_flow,
        "relaunch_with_profile",
        lambda app_arg, profile_id, window_arg, manager_arg: relaunches.append(
            (app_arg, profile_id, window_arg, manager_arg)
        ),
    )

    profile_flow.wire_profile_switch(
        app,
        window_ref=[window],
        profile_service=manager,
    )
    window.switch_profile_requested.emit()
    overlays[0].profile_selected.emit("profile-b")

    assert overlays[0].parent is window
    assert overlays[0].current_id == "profile-a"
    assert overlays[0].profiles == manager.profiles
    assert overlays[0].shown == 1
    assert overlays[0].raised == 1
    assert window.removed_filters == [overlays[0]]
    assert overlays[0].deleted == 1
    assert window.enabled == [False]
    assert relaunches == [(app, "profile-b", window, manager)]


def test_wire_profile_switch_cancel_only_removes_overlay(monkeypatch):
    overlays = []
    relaunches = []

    class _Overlay:
        def __init__(self, parent, current_id, *, profiles):
            self.parent = parent
            self.current_id = current_id
            self.profiles = profiles
            self.cancelled = _SignalStub()
            self.profile_selected = _SignalStub()
            self.create_profile_requested = _SignalStub()
            self.deleted = 0
            overlays.append(self)

        def show(self):
            pass

        def raise_(self):
            pass

        def deleteLater(self):
            self.deleted += 1

    window = _WindowStub()
    manager = _ProfileService(_Profile("Current"))
    manager.active_profile.id = "profile-a"

    monkeypatch.setattr("solin.ui.profile_switch_overlay.ProfileSwitchOverlay", _Overlay)
    monkeypatch.setattr(
        profile_flow,
        "relaunch_with_profile",
        lambda *args: relaunches.append(args),
    )

    profile_flow.wire_profile_switch(
        app=object(),
        window_ref=[window],
        profile_service=manager,
    )
    window.switch_profile_requested.emit()
    overlays[0].cancelled.emit()

    assert window.removed_filters == [overlays[0]]
    assert overlays[0].deleted == 1
    assert window.enabled == []
    assert relaunches == []
