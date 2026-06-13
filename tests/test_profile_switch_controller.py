import solin.core.profiles.manager as profile_manager
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


class _AvatarStub:
    def __init__(self):
        self.names = []

    def set_name(self, name):
        self.names.append(name)


class _Profile:
    def __init__(self, name):
        self.name = name


class _ProfileManager:
    def __init__(self, active_profile=None):
        self.active_profile = active_profile


class _WindowStub:
    def __init__(self):
        self.switch_profile_requested = _SignalStub()
        self._profile_avatar_btn = _AvatarStub()
        self.removed_filters = []
        self.enabled = []

    def removeEventFilter(self, event_filter):
        self.removed_filters.append(event_filter)

    def setEnabled(self, enabled):
        self.enabled.append(enabled)


def test_request_switch_emits_main_window_signal():
    window = _WindowStub()
    controller = ProfileSwitchController(window, lambda: _ProfileManager())

    controller.request_switch()

    assert window.switch_profile_requested.emitted == 1


def test_update_avatar_uses_active_profile_name():
    window = _WindowStub()
    manager = _ProfileManager(_Profile("Alex"))
    controller = ProfileSwitchController(window, lambda: manager)

    controller.update_avatar("profile-id")

    assert window._profile_avatar_btn.names == ["Alex"]


def test_update_avatar_ignores_missing_active_profile():
    window = _WindowStub()
    controller = ProfileSwitchController(window, lambda: _ProfileManager(None))

    controller.update_avatar("profile-id")

    assert window._profile_avatar_btn.names == []


def test_wire_profile_switch_relaunches_selected_profile_from_overlay(monkeypatch):
    overlays = []
    relaunches = []

    class _Overlay:
        def __init__(self, parent, current_id):
            self.parent = parent
            self.current_id = current_id
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
    manager = _ProfileManager(_Profile("Current"))
    manager.active_profile.id = "profile-a"

    monkeypatch.setattr(profile_manager, "get", lambda: manager)
    monkeypatch.setattr("solin.ui.profile_switch_overlay.ProfileSwitchOverlay", _Overlay)
    monkeypatch.setattr(
        profile_flow,
        "relaunch_with_profile",
        lambda app_arg, profile_id, window_arg: relaunches.append(
            (app_arg, profile_id, window_arg)
        ),
    )

    profile_flow.wire_profile_switch(
        app,
        window_ref=[window],
    )
    window.switch_profile_requested.emit()
    overlays[0].profile_selected.emit("profile-b")

    assert overlays[0].parent is window
    assert overlays[0].current_id == "profile-a"
    assert overlays[0].shown == 1
    assert overlays[0].raised == 1
    assert window.removed_filters == [overlays[0]]
    assert overlays[0].deleted == 1
    assert window.enabled == [False]
    assert relaunches == [(app, "profile-b", window)]


def test_wire_profile_switch_cancel_only_removes_overlay(monkeypatch):
    overlays = []
    relaunches = []

    class _Overlay:
        def __init__(self, parent, current_id):
            self.parent = parent
            self.current_id = current_id
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
    manager = _ProfileManager(_Profile("Current"))
    manager.active_profile.id = "profile-a"

    monkeypatch.setattr(profile_manager, "get", lambda: manager)
    monkeypatch.setattr("solin.ui.profile_switch_overlay.ProfileSwitchOverlay", _Overlay)
    monkeypatch.setattr(
        profile_flow,
        "relaunch_with_profile",
        lambda *args: relaunches.append(args),
    )

    profile_flow.wire_profile_switch(
        app=object(),
        window_ref=[window],
    )
    window.switch_profile_requested.emit()
    overlays[0].cancelled.emit()

    assert window.removed_filters == [overlays[0]]
    assert overlays[0].deleted == 1
    assert window.enabled == []
    assert relaunches == []
