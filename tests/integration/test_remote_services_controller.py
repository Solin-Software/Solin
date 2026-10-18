from types import SimpleNamespace

import pytest

from solin.controllers.remote_services_controller import RemoteServicesController


class _SignalStub:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _TimerStub:
    def __init__(self, parent=None):
        self.parent = parent
        self.timeout = _SignalStub()
        self.single_shot = None
        self.started_ms = None
        self.stopped = False

    def setSingleShot(self, single_shot):
        self.single_shot = single_shot

    def start(self, delay_ms):
        self.started_ms = delay_ms

    def stop(self):
        self.stopped = True


class _ServiceStub:
    def __init__(self):
        self.notifications_ready = _SignalStub()
        self.update_available = _SignalStub()
        self.check_calls = 0
        self.stop_calls = 0
        self.stop_args = None

    def check(self):
        self.check_calls += 1

    def stop(self, **kwargs):
        self.stop_calls += 1
        self.stop_args = kwargs


class _QueueStub:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, notifications):
        self.enqueued.append(notifications)


class _DialogStub:
    def __init__(self):
        self.finished = _SignalStub()
        self.installation_stopped = _SignalStub()
        self.shown = False
        self.invalidations = 0
        self.installer_started = False
        self.close_immediately = True

    def show(self):
        self.shown = True

    def invalidate(self):
        self.invalidations += 1
        if self.installer_started:
            return False
        if self.close_immediately:
            self.finished.emit(0)
        return True


@pytest.fixture
def remote_controller():
    timers = []

    def timer_factory(parent):
        timer = _TimerStub(parent)
        timers.append(timer)
        return timer

    notification_service = _ServiceStub()
    update_service = _ServiceStub()
    notification_queue = _QueueStub()
    dialogs = []

    def update_dialog_factory(info):
        dialog = _DialogStub()
        dialogs.append((info, dialog))
        return dialog

    parent = object()
    controller = RemoteServicesController(
        parent,
        notification_service=notification_service,
        notification_queue=notification_queue,
        update_service=update_service,
        update_dialog_factory=update_dialog_factory,
        timer_factory=timer_factory,
    )

    return SimpleNamespace(
        controller=controller, timers=timers, parent=parent,
        notification_service=notification_service, update_service=update_service,
        notification_queue=notification_queue, dialogs=dialogs,
    )


def test_remote_services_controller_start_wires_injected_services(remote_controller):
    context = remote_controller
    controller, timers = context.controller, context.timers
    notification_service, update_service = context.notification_service, context.update_service
    controller.start()

    assert len(timers) == 2
    assert all(timer.parent is context.parent for timer in timers)
    assert all(timer.single_shot is True for timer in timers)
    assert timers[0].started_ms > 0
    assert timers[1].started_ms > 0

    timers[0].timeout.emit()
    timers[1].timeout.emit()
    assert notification_service.check_calls == 1
    assert update_service.check_calls == 1

    notification_service.notifications_ready.emit(["notification"])
    assert context.notification_queue.enqueued == [["notification"]]

    update_service.update_available.emit("update-info")
    assert len(context.dialogs) == 1
    assert context.dialogs[0][0] == "update-info"
    assert context.dialogs[0][1].shown is True
    assert timers[1].stopped is True


def test_remote_services_controller_stop_stops_timers_and_services(remote_controller):
    controller = remote_controller.controller
    controller.stop()

    assert controller._notification_timer.stopped is True
    assert controller._update_timer.stopped is True
    assert controller._notification_service.stop_args == {
        "wait_ms": 0,
        "delete_when_stopped": True,
    }
    assert controller._update_service.stop_args == {
        "wait_ms": 0,
        "delete_when_stopped": True,
    }


def test_explicit_check_stops_startup_timer_before_service_check(remote_controller):
    context = remote_controller
    context.controller.start()

    def check():
        assert context.timers[1].stopped
        context.update_service.check_calls += 1

    context.update_service.check = check
    context.controller.check_updates()
    assert context.update_service.check_calls == 1
    assert context.notification_service.check_calls == 0


def test_recheck_invalidates_dialog_before_starting_service(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("old-update")
    old_dialog = context.dialogs[0][1]

    def check():
        assert old_dialog.invalidations == 1
        assert context.controller._update_dialog is None
        context.update_service.check_calls += 1

    context.update_service.check = check
    context.controller.check_updates()
    assert context.update_service.check_calls == 1
    context.update_service.update_available.emit("new-update")
    assert [info for info, _ in context.dialogs] == ["old-update", "new-update"]
    assert context.controller._update_dialog is context.dialogs[1][1]


def test_dialog_is_retained_until_finished_without_duplicate_presentations(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("update")
    dialog = context.dialogs[0][1]
    context.update_service.update_available.emit("duplicate")
    assert len(context.dialogs) == 1
    assert context.controller._update_dialog is dialog

    dialog.finished.emit(0)
    assert context.controller._update_dialog is None
    context.update_service.update_available.emit("another-update")
    next_dialog = context.dialogs[1][1]
    dialog.finished.emit(0)
    assert context.controller._update_dialog is next_dialog


def test_recheck_does_not_interrupt_an_installer_already_started(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("installing-update")
    dialog = context.dialogs[0][1]
    dialog.installer_started = True

    context.controller.check_updates()
    assert dialog.invalidations == 1
    assert context.controller._update_dialog is dialog
    assert context.update_service.check_calls == 0
    assert context.timers[1].stopped
    context.update_service.update_available.emit("another-update")
    assert len(context.dialogs) == 1


@pytest.mark.parametrize("closed", [False, True])
def test_channel_changes_during_installation_recheck_once_when_it_stops(remote_controller, closed):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("beta-update")
    dialog = context.dialogs[0][1]
    dialog.installer_started = True
    for _ in range(3):
        context.controller.check_updates()
    assert context.update_service.check_calls == 0
    dialog.installer_started = False
    if closed:
        dialog.finished.emit(0)
    else:
        dialog.installation_stopped.emit()
    assert context.update_service.check_calls == 1
    assert context.controller._update_dialog is None
    dialog.installation_stopped.emit()
    assert context.update_service.check_calls == 1
    context.update_service.update_available.emit("stable-update")
    assert [info for info, _ in context.dialogs] == ["beta-update", "stable-update"]


def test_stopping_discards_recheck_deferred_until_installation_ends(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("beta-update")
    dialog = context.dialogs[0][1]
    dialog.installer_started = True
    context.controller.check_updates()
    context.controller.stop()
    dialog.installer_started = False
    dialog.installation_stopped.emit()
    dialog.finished.emit(0)
    assert context.update_service.check_calls == 0


def test_async_invalidation_waits_for_close_then_presents_latest_result(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("old-update")
    dialog = context.dialogs[0][1]
    dialog.close_immediately = False

    context.controller.check_updates()
    context.update_service.update_available.emit("intermediate-update")
    context.update_service.update_available.emit("latest-update")
    assert context.controller._update_dialog is dialog
    assert len(context.dialogs) == 1

    dialog.finished.emit(0)
    assert [info for info, _ in context.dialogs] == ["old-update", "latest-update"]
    assert context.controller._update_dialog is context.dialogs[1][1]


def test_repeated_recheck_discards_result_waiting_for_dialog_close(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("old-update")
    dialog = context.dialogs[0][1]
    dialog.close_immediately = False

    context.controller.check_updates()
    context.update_service.update_available.emit("superseded-update")
    context.controller.check_updates()
    dialog.finished.emit(0)
    assert len(context.dialogs) == 1
    assert context.controller._update_dialog is None
    assert dialog.invalidations == 1
    assert context.update_service.check_calls == 2

    context.update_service.update_available.emit("latest-update")
    assert context.dialogs[-1][0] == "latest-update"


def test_start_is_idempotent(remote_controller):
    context = remote_controller
    context.controller.start()
    context.controller.start()
    context.timers[0].timeout.emit()
    context.timers[1].timeout.emit()
    context.notification_service.notifications_ready.emit(["notification"])
    assert context.notification_service.check_calls == 1
    assert context.update_service.check_calls == 1
    assert context.notification_queue.enqueued == [["notification"]]


def test_stop_invalidates_dialog_and_suppresses_pending_results_and_restart(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("old-update")
    dialog = context.dialogs[0][1]
    dialog.close_immediately = False
    context.controller.check_updates()
    context.update_service.update_available.emit("pending-update")

    context.controller.stop()
    dialog.finished.emit(0)
    context.update_service.update_available.emit("queued-update")
    context.controller.check_updates()
    context.controller.start()
    context.timers[1].timeout.emit()

    assert len(context.dialogs) == 1
    assert context.controller._update_dialog is None
    assert dialog.invalidations == 2
    assert context.update_service.check_calls == 1


def test_stop_is_idempotent_even_with_a_dialog_closing_asynchronously(remote_controller):
    context = remote_controller
    context.controller.start()
    context.update_service.update_available.emit("update")
    dialog = context.dialogs[0][1]
    dialog.close_immediately = False
    context.controller.stop()
    context.controller.stop()
    assert dialog.invalidations == 1
    assert context.notification_service.stop_calls == 1
    assert context.update_service.stop_calls == 1
