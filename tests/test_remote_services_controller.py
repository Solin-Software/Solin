from solin.controllers.remote_services_controller import RemoteServicesController


class _TimerStub:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _ServiceStub:
    def __init__(self):
        self.stop_args = None

    def stop(self, **kwargs):
        self.stop_args = kwargs


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
