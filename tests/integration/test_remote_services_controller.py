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
        self.stop_args = None

    def check(self):
        self.check_calls += 1

    def stop(self, **kwargs):
        self.stop_args = kwargs


class _QueueStub:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, notifications):
        self.enqueued.append(notifications)


class _DialogStub:
    def __init__(self):
        self.shown = False

    def show(self):
        self.shown = True


def test_remote_services_controller_start_wires_injected_services():
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

    controller.start()

    assert len(timers) == 2
    assert all(timer.parent is parent for timer in timers)
    assert all(timer.single_shot is True for timer in timers)
    assert timers[0].started_ms > 0
    assert timers[1].started_ms > 0

    timers[0].timeout.emit()
    timers[1].timeout.emit()
    assert notification_service.check_calls == 1
    assert update_service.check_calls == 1

    notification_service.notifications_ready.emit(["notification"])
    assert notification_queue.enqueued == [["notification"]]

    update_service.update_available.emit("update-info")
    assert len(dialogs) == 1
    assert dialogs[0][0] == "update-info"
    assert dialogs[0][1].shown is True


def test_remote_services_controller_stop_stops_timers_and_services():
    controller = RemoteServicesController.__new__(RemoteServicesController)
    controller._notification_timer = _TimerStub()
    controller._update_timer = _TimerStub()
    controller._notification_service = _ServiceStub()
    controller._update_service = _ServiceStub()

    controller.stop()

    assert controller._notification_timer.stopped is True
    assert controller._update_timer.stopped is True
    assert controller._notification_service.stop_args == {
        "wait_ms": 100,
        "delete_when_stopped": True,
    }
    assert controller._update_service.stop_args == {
        "wait_ms": 100,
        "delete_when_stopped": True,
    }
