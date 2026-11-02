from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from solin.bootstrap import application
from solin.bootstrap.runtime_args import parse_runtime_args


class _Screen(QWidget):
    profile_ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.start = Mock()
        self.start_new_profile = Mock()


@pytest.fixture
def bootstrap(monkeypatch):
    screen = _Screen()
    current = SimpleNamespace(id="current")
    profiles = SimpleNamespace(
        profiles=[current],
        get_profile=lambda identifier: current if identifier == "current" else None,
        restore_last_active=Mock(return_value="current"),
        set_active=Mock(),
    )
    container = SimpleNamespace(profile_service=profiles, window_ref=[None])
    create_screen = Mock(return_value=screen)
    launch = Mock()
    monkeypatch.setattr(application, "_create_profile_screen", create_screen)
    monkeypatch.setattr(application, "_launch_profile_window", launch)
    try:
        yield SimpleNamespace(
            screen=screen,
            container=container,
            profiles=profiles,
            launch=launch,
            create_screen=create_screen,
        )
    finally:
        screen.close()
        screen.deleteLater()


def test_forced_selector_bypasses_one_profile_without_changing_next_launch(bootstrap):
    state = bootstrap
    files = ["pending.mp4"]
    application._start_profile_flow(
        state.container,
        "lang",
        parse_runtime_args(["solin", "--select-profile"]),
        files,
    )
    state.launch.assert_not_called()
    state.profiles.set_active.assert_not_called()
    state.screen.start.assert_called_once_with(force_selector=True)
    application._start_profile_flow(state.container, "lang", parse_runtime_args(["solin"]), files)
    state.launch.assert_called_once_with(state.container, "lang", files, "current")
    state.profiles.set_active.assert_called_once_with("current")


@pytest.mark.parametrize(
    "arguments",
    [
        ["--profile", "current"],
        ["--create-profile"],
        ["--profile", "current", "--create-profile"],
    ],
)
def test_forced_selector_takes_precedence_over_other_startup_modes(bootstrap, arguments):
    state = bootstrap
    application._start_profile_flow(
        state.container,
        "lang",
        parse_runtime_args(["solin", *arguments, "--select-profile"]),
        [],
    )
    state.launch.assert_not_called()
    state.screen.start.assert_called_once_with(force_selector=True)
    state.screen.start_new_profile.assert_not_called()


def test_creation_starts_onboarding_and_preserves_shared_pending_file_list(bootstrap, monkeypatch):
    state = bootstrap
    files = ["pending.mp4"]
    monkeypatch.setattr(application.QTimer, "singleShot", lambda _delay, callback: callback())
    application._start_profile_flow(
        state.container,
        "lang",
        parse_runtime_args(["solin", "--create-profile"]),
        files,
    )
    state.screen.start_new_profile.assert_called_once()
    state.screen.start.assert_not_called()
    state.screen.profile_ready.emit("new-profile")
    state.launch.assert_called_once_with(state.container, "lang", files, "new-profile")


@pytest.mark.parametrize("profile_count", [0, 2])
def test_normal_startup_uses_selector_for_zero_or_multiple_profiles(bootstrap, profile_count):
    state = bootstrap
    state.profiles.profiles = [SimpleNamespace(id=str(i)) for i in range(profile_count)]
    application._start_profile_flow(state.container, "lang", parse_runtime_args(["solin"]), [])
    state.launch.assert_not_called()
    state.screen.start.assert_called_once_with(force_selector=False)


@pytest.mark.parametrize("handoff", [None, "channel"])
def test_bootstrap_admission_precedes_container_and_rejected_entry_is_non_mutating(
    monkeypatch, handoff
):
    app = Mock()
    router = Mock()
    server = Mock()
    server.start.return_value = False
    initialize = Mock()
    wait = Mock(return_value=None)
    catalogs = Mock()
    args = ["solin"] if handoff is None else ["solin", "--profile-handoff", handoff]
    monkeypatch.setattr(application.sys, "argv", args)
    monkeypatch.setattr(application, "QApplication", lambda _args: app)
    monkeypatch.setattr(application, "default_app_config", Mock())
    monkeypatch.setattr(application, "ApplicationFileOpenRouter", Mock(return_value=router))
    monkeypatch.setattr(application, "SingleInstanceServer", Mock(return_value=server))
    monkeypatch.setattr(application, "try_forward_to_running", Mock(return_value=False))
    monkeypatch.setattr(application, "initialize_application_container", initialize)
    monkeypatch.setattr(application, "prepare_application_catalogs", catalogs)
    monkeypatch.setattr(application, "wait_for_profile_handoff", wait)
    application.main()
    initialize.assert_not_called()
    catalogs.assert_not_called()
    if handoff is not None:
        wait.assert_called_once_with(handoff)
        server.start.assert_not_called()
    else:
        server.start.assert_called_once()
        router.close.assert_called_once()


def test_container_failure_closes_admitted_ipc_and_file_router(monkeypatch):
    events = []
    router = SimpleNamespace(close=lambda: events.append("router_closed"))
    server = SimpleNamespace(
        start=lambda: events.append("ipc_started") or True,
        restore_request_ids=lambda identifiers: None,
        close=lambda: events.append("ipc_closed"),
    )
    monkeypatch.setattr(application.sys, "argv", ["solin"])
    monkeypatch.setattr(application, "QApplication", Mock())
    monkeypatch.setattr(application, "default_app_config", Mock())
    monkeypatch.setattr(application, "ApplicationFileOpenRouter", lambda *args: router)
    monkeypatch.setattr(application, "SingleInstanceServer", lambda *args, **kwargs: server)
    monkeypatch.setattr(application, "try_forward_to_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(application, "prepare_application_catalogs", lambda: None)

    def initialize(*_args):
        assert events == ["ipc_started"]
        raise OSError("failed to initialize")

    monkeypatch.setattr(application, "initialize_application_container", initialize)
    with pytest.raises(OSError, match="failed to initialize"):
        application.main()
    assert events == ["ipc_started", "router_closed", "ipc_closed"]
