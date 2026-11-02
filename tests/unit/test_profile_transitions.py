from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from solin.bootstrap import profile_flow
from solin.bootstrap.application_window import ApplicationWindow, ApplicationWindowState


class _Runtime(QWidget):
    first_frame_presented = Signal()
    switch_profile_requested = Signal()

    def __init__(self, parent, events):
        super().__init__(parent)
        self.events = events
        self.allow_close = True
        self.confirmations = 0
        self.on_confirm = lambda: None
        self.on_shutdown = lambda: None

    def can_leave_profile(self):
        return self.allow_close

    def confirm_close(self):
        self.confirmations += 1
        self.on_confirm()
        return self.allow_close

    def commit_construction(self):
        pass

    def complete_startup_handoff(self):
        pass

    def open_media_files(self, paths):
        self.events.append(("files", paths))

    def shutdown(self):
        self.events.append("runtime_shutdown")
        self.on_shutdown()


class _Handoff(QObject):
    ready = Signal()
    prepared = Signal()
    failed = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.closed = False
        self.acknowledge = True
        self.pending_files = []
        self.batches = []
        self.events = parent._window.runtime.events

    def start(self):
        return "test-channel"

    def close(self):
        self.closed = True

    def prepare(self, pending_files, accepted_request_ids):
        self.pending_files.extend(pending_files)
        self.batches.append(list(pending_files))
        self.accepted_request_ids = accepted_request_ids
        if self.acknowledge:
            self.prepared.emit()

    def release(self):
        self.events.append("release")
        return True


@pytest.fixture
def transition(monkeypatch):
    application = QApplication.instance()
    events = []
    pending_files = []
    window = ApplicationWindow(
        width=900,
        height=700,
        geometry=QByteArray(),
        save_geometry=lambda _geometry: events.append("geometry"),
        pending_files=pending_files,
    )
    controller = None
    try:
        window.show()
        window.begin_hydration()
        runtime = _Runtime(window.content_parent(), events)
        window.register_runtime_candidate(runtime)
        window.install_runtime(runtime)
        runtime.first_frame_presented.emit()
        window.complete_startup_handoff()
        current = SimpleNamespace(id="current", name="Current")
        target = SimpleNamespace(id="target", name="Target")
        profiles = SimpleNamespace(
            active_profile=current,
            profiles=[current, target],
            get_profile=lambda profile_id: next(
                (profile for profile in (current, target) if profile.id == profile_id),
                None,
            ),
            prepare_profile_creation=Mock(),
        )
        app = SimpleNamespace(
            quit=lambda: events.append("quit"),
            quitOnLastWindowClosed=application.quitOnLastWindowClosed,
            setQuitOnLastWindowClosed=application.setQuitOnLastWindowClosed,
        )
        monkeypatch.setattr(profile_flow, "ProfileHandoff", _Handoff)
        launch = Mock(return_value=(True, 123))
        monkeypatch.setattr(profile_flow.QProcess, "startDetached", launch)
        controller = profile_flow.wire_profile_switch(
            app,
            [window],
            profiles,
            shutdown=lambda: events.append("app_shutdown"),
            notify_failure=Mock(),
        )
        yield SimpleNamespace(
            app=app,
            window=window,
            runtime=runtime,
            profiles=profiles,
            controller=controller,
            launch=launch,
            events=events,
            pending_files=pending_files,
        )
    finally:
        if controller is not None and controller._handoff is not None:
            controller._handoff.close()
        window.cancel_profile_transition()
        runtime = window.runtime
        if runtime is not None:
            runtime.allow_close = True
            runtime.on_shutdown = lambda: None
            runtime.on_confirm = lambda: None
        window.close()
        window.deleteLater()


@pytest.mark.parametrize(
    "action,flag",
    [
        ("select_profile", "--profile"),
        ("create_profile", "--create-profile"),
        ("logout", "--select-profile"),
    ],
)
def test_successor_waits_until_runtime_and_application_cleanup(transition, action, flag):
    state = transition
    callback = getattr(state.controller, action)
    callback("target") if action == "select_profile" else callback()
    args = state.launch.call_args.args[1]
    assert flag in args
    assert args[-2:] == ["--profile-handoff", "test-channel"]
    assert state.events == []
    assert state.window.state is ApplicationWindowState.READY
    assert not state.window.isEnabled()
    assert state.runtime.confirmations == 1
    state.controller._handoff.ready.emit()
    assert state.events == ["geometry", "runtime_shutdown", "app_shutdown", "release", "quit"]
    assert state.runtime.confirmations == 1
    assert state.window.state is ApplicationWindowState.CLOSING


@pytest.mark.parametrize("failure", [(False, -1), RuntimeError("launch failed")])
def test_launch_failure_preserves_runtime_and_active_ipc(transition, failure):
    state = transition
    ipc = Mock()
    state.app._solin_single_instance_server = ipc
    if isinstance(failure, Exception):
        state.launch.side_effect = failure
    else:
        state.launch.return_value = failure
    state.controller.select_profile("target")
    assert state.events == []
    assert state.window.isEnabled()
    assert state.window.profile_transition_available
    assert state.window.isVisible()
    ipc.close.assert_not_called()
    assert state.profiles.active_profile.id == "current"
    state.controller._notify_failure.assert_called_once()
    # A failed launch cannot leave a cached close approval.
    state.runtime.allow_close = False
    assert not state.window.close()
    assert state.runtime.confirmations == 2


@pytest.mark.parametrize("action", ["select_profile", "create_profile", "logout"])
def test_recording_or_unsaved_editor_can_veto_each_transition(transition, action):
    state = transition
    state.runtime.allow_close = False
    callback = getattr(state.controller, action)
    callback("target") if action == "select_profile" else callback()
    state.launch.assert_not_called()
    assert state.events == []
    assert state.window.isEnabled()
    assert state.window.profile_transition_available
    state.profiles.prepare_profile_creation.assert_not_called()


def test_invalid_and_current_destinations_do_not_disable_or_launch(transition):
    state = transition
    state.controller.select_profile("missing")
    state.controller.select_profile("current")
    state.launch.assert_not_called()
    assert state.window.isEnabled()
    assert state.runtime.confirmations == 0


def test_transition_guard_covers_nested_confirmation_and_repeated_activation(transition):
    state = transition
    state.runtime.on_confirm = state.controller.logout
    state.controller.select_profile("target")
    state.controller.create_profile()
    state.controller.logout()
    state.controller.select_profile("target")
    assert state.launch.call_count == 1
    assert state.runtime.confirmations == 1
    assert not state.window.close()  # The titlebar cannot bypass the pending handoff.
    state.controller._handoff.failed.emit("child exited")
    assert state.window.profile_transition_available
    assert state.window.isEnabled()
    assert state.events == []


def test_rejected_commit_aborts_child_and_restores_session(transition, monkeypatch):
    state = transition
    state.controller.logout()
    handoff = state.controller._handoff
    monkeypatch.setattr(state.window, "close_for_profile_transition", lambda: False)
    handoff.ready.emit()
    assert handoff.closed
    assert state.window.profile_transition_available
    assert state.window.isEnabled()
    assert state.events == []


def test_shutdown_failure_never_releases_child(transition):
    state = transition
    state.runtime.on_shutdown = Mock(side_effect=RuntimeError("shutdown failed"))
    state.controller.logout()
    handoff = state.controller._handoff
    handoff.ready.emit()
    assert handoff.closed
    assert "release" not in state.events


def test_ordinary_shutdown_error_does_not_leave_an_unclosable_window(transition):
    state = transition
    state.runtime.on_shutdown = Mock(side_effect=RuntimeError("shutdown failed"))

    assert state.window.close()
    assert not state.window.isVisible()
    assert "runtime_shutdown" in state.events
    assert state.window.close()
    state.launch.assert_not_called()


def test_recording_started_during_handoff_aborts_without_confirming_twice(transition):
    state = transition
    state.controller.logout()
    handoff = state.controller._handoff
    state.runtime.allow_close = False
    handoff.ready.emit()
    assert handoff.closed
    assert state.runtime.confirmations == 1
    assert state.events == []
    assert state.window.profile_transition_available
    assert state.window.isEnabled()


def test_ordinary_close_confirmation_blocks_nested_profile_transition(transition):
    state = transition
    state.runtime.on_confirm = state.controller.logout
    state.window.close()
    state.launch.assert_not_called()
    assert state.runtime.confirmations == 1
    assert state.events == ["geometry", "runtime_shutdown"]


def test_geometry_failure_does_not_skip_resource_shutdown(transition, monkeypatch):
    state = transition
    monkeypatch.setattr(state.window, "_save_geometry", Mock(side_effect=OSError("disk full")))
    state.controller.logout()
    state.controller._handoff.ready.emit()
    assert state.events == ["runtime_shutdown", "app_shutdown", "release", "quit"]


def test_closing_window_cannot_start_another_transition(transition):
    state = transition
    state.window.close()
    state.controller.logout()
    state.controller.open_selector()
    state.launch.assert_not_called()
    assert state.controller._overlay is None


def test_file_requests_wait_and_follow_the_successor(transition):
    state = transition
    state.controller.logout()
    state.window.open_media_files(["late.mp4"])
    handoff = state.controller._handoff
    assert state.events == []
    assert not state.window.accepts_external_activation
    handoff.ready.emit()
    assert handoff.pending_files == ["late.mp4"]


def test_ipc_admission_stops_before_shutdown_and_acceptance_follows_successor(transition):
    state = transition
    ipc = Mock()
    ipc.accepted_request_ids = ["a" * 32]
    state.app._solin_single_instance_server = ipc
    ipc.pause.side_effect = lambda: state.window.open_media_files(["accepted.mp4"])

    def shutdown():
        ipc.pause.assert_called_once()
        assert state.events == ["geometry", "runtime_shutdown"]

    state.runtime.on_shutdown = shutdown
    state.controller.logout()
    handoff = state.controller._handoff
    handoff.ready.emit()

    assert handoff.pending_files == ["accepted.mp4"]
    assert handoff.accepted_request_ids == ["a" * 32]
    ipc.reserve_until_exit.assert_called_once()
    ipc.start.assert_not_called()


def test_late_recording_veto_resumes_ipc_and_delivers_accepted_files(transition):
    state = transition
    ipc = Mock()
    state.app._solin_single_instance_server = ipc
    ipc.pause.side_effect = lambda: state.window.open_media_files(["accepted.mp4"])
    state.controller.logout()
    handoff = state.controller._handoff
    state.runtime.allow_close = False
    handoff.ready.emit()

    ipc.start.assert_called_once()
    ipc.reserve_until_exit.assert_not_called()
    assert state.events == [("files", ["accepted.mp4"])]
    assert state.window.isEnabled()
    assert handoff.closed


def test_recovery_delivers_queued_file_requests_to_current_runtime(transition):
    state = transition
    state.controller.logout()
    state.window.open_media_files(["late.mp4"])
    state.controller._handoff.failed.emit("replacement disconnected")
    assert state.events == [("files", ["late.mp4"])]
    assert state.window.accepts_external_activation


@pytest.mark.parametrize("failure", ["capacity exceeded", "write failed", "peer disconnected", "timeout"])
def test_preparation_failure_preserves_the_current_session_and_accepted_files(transition, failure):
    state = transition
    ipc = Mock()
    ipc.accepted_request_ids = ["a" * 32]
    state.app._solin_single_instance_server = ipc
    state.controller.logout()
    paths = ["accepted.mp4"]
    state.window.open_media_files(paths)
    handoff = state.controller._handoff
    handoff.acknowledge = False
    handoff.ready.emit()
    assert state.events == []
    assert state.window.pending_files == paths
    handoff.failed.emit(failure)

    assert state.window.state is ApplicationWindowState.READY
    assert state.window.isEnabled()
    assert state.events == [("files", paths)]
    state.controller._notify_failure.assert_called_once()
    ipc.start.assert_called_once()
    ipc.reserve_until_exit.assert_not_called()


def test_shutdown_waits_for_successor_to_acknowledge_pending_work(transition):
    state = transition
    state.controller.logout()
    state.window.open_media_files(["accepted.mp4"])
    handoff = state.controller._handoff
    handoff.acknowledge = False
    handoff.ready.emit()

    assert state.window.state is ApplicationWindowState.READY
    assert state.window.pending_files == ["accepted.mp4"]
    assert state.events == []
    handoff.prepared.emit()
    assert state.events == ["geometry", "runtime_shutdown", "app_shutdown", "release", "quit"]
    assert state.window.pending_files == []


def test_native_arrivals_follow_each_ack_and_shutdown_without_loss(transition, tmp_path):
    from PySide6.QtGui import QFileOpenEvent
    from solin.bootstrap.file_open import ApplicationFileOpenRouter

    state = transition
    router = ApplicationFileOpenRouter(QApplication.instance(), [state.window], state.pending_files)
    paths = [str(tmp_path / f"arrival-{index}.mp4") for index in range(4)]
    for index in range(4):
        (tmp_path / f"arrival-{index}.mp4").touch()

    def arrive(index):
        router.eventFilter(QApplication.instance(), QFileOpenEvent(paths[index]))

    try:
        state.controller.logout()
        state.window.open_media_files([paths[0]])
        handoff = state.controller._handoff
        handoff.acknowledge = False
        handoff.ready.emit()
        arrive(1)
        handoff.prepared.emit()
        assert handoff.batches == [[paths[0]], [paths[1]]]
        assert state.events == []

        state.runtime.on_shutdown = lambda: arrive(2)
        handoff.prepared.emit()
        assert handoff.batches[-1] == [paths[2]]
        assert "quit" not in state.events
        arrive(3)
        handoff.prepared.emit()
        assert handoff.batches[-1] == [paths[3]]
        assert "release" not in state.events
        handoff.prepared.emit()

        assert handoff.pending_files == paths
        assert state.pending_files == []
        assert state.events == ["geometry", "runtime_shutdown", "app_shutdown", "release", "quit"]
    finally:
        router.close()


def test_failed_late_ack_quits_without_releasing_an_unprepared_successor(transition):
    state = transition
    quit_policy = state.app.quitOnLastWindowClosed()
    state.controller.logout()
    handoff = state.controller._handoff

    def during_shutdown():
        handoff.acknowledge = False
        state.window.open_media_files(["late.mp4"])

    state.runtime.on_shutdown = during_shutdown
    handoff.ready.emit()
    assert "quit" not in state.events
    handoff.failed.emit("peer exited after shutdown")
    assert handoff.closed
    assert "release" not in state.events
    assert state.events[-1] == "quit"
    assert state.app.quitOnLastWindowClosed() == quit_policy


def test_wheel_installation_relaunches_an_installed_module(monkeypatch, tmp_path):
    monkeypatch.setattr(
        profile_flow,
        "__file__",
        str(tmp_path / "site-packages/solin/bootstrap/profile_flow.py"),
    )
    monkeypatch.delattr(profile_flow.sys, "frozen", raising=False)
    program, arguments, _directory = profile_flow.detached_launch_command(["--select-profile"])
    assert program == profile_flow.sys.executable
    assert arguments == ["-m", "solin.bootstrap.application", "--select-profile"]
