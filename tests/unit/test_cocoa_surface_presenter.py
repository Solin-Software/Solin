"""Qt presenter ownership, timer, replacement and native-failure recovery."""

from __future__ import annotations

import sys
from itertools import pairwise
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Signal
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QWidget
import pytest
import shiboken6

from solin.controllers.cocoa_surface_presenter import CocoaSurfacePresenterController


def test_offscreen_window_id_is_never_passed_as_a_cocoa_view(monkeypatch, native):
    from solin.widgets.projection.native_surface import NativeVideoSurface

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("PySide6.QtWidgets.QApplication.platformName", lambda: "offscreen")
    surface = NativeVideoSurface()
    try:
        assert surface.surface_endpoint == ""
        native.factory.assert_not_called()
    finally:
        surface.deleteLater()


class _Surface(QWidget):
    geometry_changed = Signal()

    def __init__(self):
        super().__init__()
        self.address = 100

    @property
    def native_handle(self):
        return self.address


@pytest.fixture
def native(monkeypatch):
    created = []
    calls = []

    def main_thread():
        assert QThread.currentThread() == QCoreApplication.instance().thread()

    def create(handle):
        main_thread()
        presenter = SimpleNamespace(endpoint=f"org.pylibobs.surfaces.test-{len(created)}")

        def poll():
            main_thread()
            calls.append("poll")
            return True

        def close():
            main_thread()
            calls.append("close")

        presenter.poll = Mock(side_effect=poll)
        presenter.close = Mock(side_effect=close)
        created.append(presenter)
        return presenter

    module = ModuleType("pylibobs.macos_surfaces")
    module.SurfacePresenter = Mock(side_effect=create)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(created=created, factory=module.SurfacePresenter, calls=calls)


@pytest.fixture
def surface():
    widget = _Surface()
    yield widget
    if shiboken6.isValid(widget):
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def clock(monkeypatch):
    """Drive owned Qt timers without sleeping or dispatching unrelated events."""
    state = SimpleNamespace(now=0.0, timers=[])

    class Timer(QObject):
        timeout = Signal()

        def __init__(self, parent):
            super().__init__(parent)
            self._interval = 0
            self._single_shot = False
            self.deadline = None
            state.timers.append(self)

        def setInterval(self, milliseconds):
            self._interval = milliseconds

        def setSingleShot(self, single_shot):
            self._single_shot = single_shot

        def start(self, milliseconds=None):
            if milliseconds is not None:
                self.setInterval(milliseconds)
            self.deadline = state.now + self._interval / 1000

        def stop(self):
            self.deadline = None

        def isActive(self):
            return shiboken6.isValid(self) and self.deadline is not None

    def advance(seconds):
        end = state.now + seconds
        while pending := [
            timer for timer in state.timers if timer.isActive() and timer.deadline <= end
        ]:
            timer = min(pending, key=lambda item: item.deadline)
            state.now = timer.deadline
            if timer._single_shot:
                timer.stop()
            else:
                timer.start()
            timer.timeout.emit()
        state.now = end

    state.advance = advance
    monkeypatch.setattr("solin.controllers.cocoa_surface_presenter.QTimer", Timer)
    return state


def test_persistent_poll_failure_has_spaced_bounded_retries(surface, native, clock):
    surface.show()
    created_at = []
    factory = native.factory.side_effect

    def create(handle):
        created_at.append(clock.now)
        presenter = factory(handle)
        presenter.poll.side_effect = RuntimeError("persistent graphics failure")
        return presenter

    native.factory.side_effect = create
    controller = CocoaSurfacePresenterController(surface)
    endpoints = [controller.endpoint]
    failures = []

    def reconnect():
        try:
            endpoints.append(controller.endpoint)
        except RuntimeError:
            failures.append(True)

    surface.geometry_changed.connect(reconnect)

    clock.advance(0.250)
    assert len(native.created) <= 2, "Poll failures must not recreate a view every 8 ms"
    clock.advance(20)
    assert 1 < len(native.created) <= 4
    assert all(later - earlier >= 0.250 for earlier, later in pairwise(created_at))
    assert len(set(endpoints)) == len(native.created)
    assert failures == [True]
    for presenter in native.created:
        presenter.close.assert_called_once()
    assert not any(timer.isActive() for timer in clock.timers)

    attempts = native.factory.call_count
    for _ in range(100):
        with pytest.raises(RuntimeError):
            _ = controller.endpoint
        controller._retry()
    clock.advance(60)
    assert native.factory.call_count == attempts


def test_constructor_failures_share_retry_budget_and_delay_boundaries(surface, native, clock):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    native.factory.side_effect = RuntimeError("native allocation failed")
    changed = QSignalSpy(surface.geometry_changed)
    with pytest.raises(RuntimeError):
        _ = controller.endpoint

    for attempt, delay in enumerate((0.250, 1.000, 4.000), start=1):
        for _ in range(10):
            controller.set_visible(True)
            with pytest.raises(RuntimeError):
                _ = controller.endpoint
        assert native.factory.call_count == attempt
        clock.advance(delay - 0.001)
        assert native.factory.call_count == attempt
        clock.advance(0.001)
        assert native.factory.call_count == attempt + 1

    assert changed.count() == 1
    assert not any(timer.isActive() for timer in clock.timers)
    for _ in range(10):
        with pytest.raises(RuntimeError):
            _ = controller.endpoint
    clock.advance(60)
    assert native.factory.call_count == 4


@pytest.mark.parametrize("presented_frames", [0, 59])
def test_empty_polls_and_short_frame_runs_do_not_renew_budget(
    surface,
    native,
    clock,
    presented_frames,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    for delay in (0.250, 1.000, 4.000, None):
        presenter = native.created[-1]
        presenter.poll.side_effect = None
        presenter.poll.return_value = False
        # Idle time far longer than all retry delays is still not recovery.
        clock.advance(10)
        presenter.poll.return_value = True
        for _ in range(presented_frames):
            controller._poll()
        presenter.poll.side_effect = RuntimeError("frame presentation failed")
        controller._poll()
        if delay is not None:
            clock.advance(delay)
    assert len(native.created) == 4
    assert not any(timer.isActive() for timer in clock.timers)


def test_delivered_frames_renew_the_budget_after_temporary_failure(surface, native, clock):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    native.created[0].poll.side_effect = RuntimeError("temporary context loss")
    controller._poll()
    clock.advance(0.250)
    recovered = native.created[-1]
    # Empty polls between delivered frames are normal at a faster polling rate.
    recovered.poll.side_effect = [value for _ in range(60) for value in (False, True)]
    for _ in range(120):
        controller._poll()
    recovered.poll.side_effect = RuntimeError("a later, independent context loss")
    controller._poll()
    clock.advance(0.249)
    assert len(native.created) == 2
    clock.advance(0.001)
    assert len(native.created) == 3
    assert controller._timer.isActive()


@pytest.mark.parametrize("action", ["hide", "close", "dispose"])
def test_pending_recovery_is_cancelled_by_lifetime_changes(surface, native, clock, action):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    old = native.created[0]
    old.poll.side_effect = RuntimeError("context lost")
    controller._poll()
    assert any(timer.isActive() for timer in clock.timers)

    if action == "hide":
        surface.hide()
        controller.set_visible(False)
    elif action == "close":
        controller.close()
        controller.close()
    else:
        surface.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(controller)
    if action != "dispose":
        # Even a stale timeout delivered after cancellation cannot recreate it.
        controller._retry()
    clock.advance(60)
    native.factory.assert_called_once()
    old.close.assert_called_once()
    assert not any(timer.isActive() for timer in clock.timers)


def test_initial_constructor_failure_falls_back_until_explicit_visibility_recovery(
    surface,
    native,
    clock,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    factory = native.factory.side_effect
    native.factory.side_effect = RuntimeError("temporary allocation failure")
    with pytest.raises(RuntimeError):
        _ = controller.endpoint
    changed = QSignalSpy(surface.geometry_changed)
    # Match the existing caller: construction errors immediately hide the native
    # surface and cancel automatic retries while its fallback remains visible.
    surface.hide()
    controller.set_visible(False)
    native.factory.side_effect = factory
    clock.advance(20)
    assert native.factory.call_count == 1
    assert changed.count() == 0
    for _ in range(10):
        with pytest.raises(RuntimeError):
            _ = controller.endpoint
    assert native.factory.call_count == 1

    endpoints = []
    surface.geometry_changed.connect(lambda: endpoints.append(controller.endpoint))
    surface.show()
    controller.set_visible(True)
    clock.advance(0)
    assert endpoints == [controller.endpoint]
    assert len(native.created) == 1
    assert controller._timer.isActive()


@pytest.mark.parametrize("read_endpoint", [False, True])
def test_recovery_uses_replaced_handle_and_notifies_targets(
    surface,
    native,
    clock,
    read_endpoint,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    original = controller.endpoint
    native.created[0].poll.side_effect = RuntimeError("old view invalidated")
    controller._poll()
    surface.address = 200
    endpoints = []
    surface.geometry_changed.connect(lambda: endpoints.append(controller.endpoint))
    if read_endpoint:
        assert controller.endpoint != original
    else:
        clock.advance(0.250)
    assert endpoints == [controller.endpoint]
    native.factory.assert_called_with(200)
    assert len(native.created) == 2
    native.created[0].close.assert_called_once()
    assert not controller._retry_timer.isActive()


@pytest.mark.parametrize("action", ["hide", "close", "dispose"])
def test_replacement_notification_can_reentrantly_end_presentation(
    surface,
    native,
    clock,
    action,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    native.created[0].poll.side_effect = RuntimeError("old context lost")
    endpoints = []

    def changed():
        endpoints.append(controller.endpoint)
        if action == "hide":
            surface.hide()
            controller.set_visible(False)
        elif action == "close":
            controller.close()
        else:
            surface.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    surface.geometry_changed.connect(changed)
    controller._poll()
    clock.advance(60)
    assert endpoints == [native.created[1].endpoint]
    assert len(native.created) == 2
    assert not any(timer.isActive() for timer in clock.timers)
    native.created[0].close.assert_called_once()
    if action != "hide":
        native.created[1].close.assert_called_once()


def test_hidden_presenter_is_lazy_and_does_not_start_timer(surface, native):
    controller = CocoaSurfacePresenterController(surface)
    native.factory.assert_not_called()
    controller.set_visible(True)
    assert not controller._timer.isActive()
    endpoint = controller.endpoint
    assert endpoint.startswith("org.pylibobs.surfaces.")
    assert not controller._timer.isActive()
    assert controller.endpoint == endpoint
    native.factory.assert_called_once_with(surface.native_handle)


def test_visible_presenter_polls_on_qt_main_thread_and_hidden_stops_timer(surface, native):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    assert controller._timer.isActive()
    fired = QSignalSpy(controller._timer.timeout)
    assert fired.wait(1000), "Visible presenter timer did not fire"
    assert native.created[0].poll.call_count > 0
    controller.set_visible(False)
    assert not controller._timer.isActive()
    count = native.created[0].poll.call_count
    QTest.qWait(20)
    assert native.created[0].poll.call_count == count
    controller.set_visible(True)
    assert controller._timer.isActive()


def test_automatic_recovery_runs_on_owning_qt_thread(surface, native):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    original = controller.endpoint
    old = native.created[0]
    old.poll.side_effect = RuntimeError("temporary graphics failure")
    endpoints = []
    surface.geometry_changed.connect(lambda: endpoints.append(controller.endpoint))
    retried = QSignalSpy(controller._retry_timer.timeout)

    controller._poll()
    assert retried.wait(2000), "Visible presenter recovery timer did not fire"
    assert endpoints == [native.created[1].endpoint]
    assert endpoints[0] != original
    assert controller._timer.isActive()
    old.close.assert_called_once()


def test_new_native_handle_after_reparent_replaces_endpoint_and_closes_old_view(surface, native):
    first_parent, second_parent = QWidget(), QWidget()
    try:
        surface.setParent(first_parent)
        controller = CocoaSurfacePresenterController(surface)
        initial = controller.endpoint
        old = native.created[0]
        surface.setParent(second_parent)
        # A toolkit may replace the NSView while reparenting the host.
        surface.address = 200
        replacement = controller.endpoint
        assert replacement != initial
        old.close.assert_called_once()
        assert native.factory.call_args.args == (200,)
        assert controller.parent() is surface
    finally:
        surface.setParent(None)
        first_parent.deleteLater()
        second_parent.deleteLater()


def test_poll_failure_closes_native_view_before_requesting_new_endpoint(surface, native, clock):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    first_endpoint = controller.endpoint
    old = native.created[0]
    old.poll.side_effect = RuntimeError("consumer lost graphics context")
    requested = []

    def reconnect():
        old.close.assert_called_once()
        requested.append(controller.endpoint)

    surface.geometry_changed.connect(reconnect)
    controller._poll()
    old.close.assert_called_once()
    assert requested == []
    assert not controller._timer.isActive()
    assert controller.endpoint == first_endpoint
    clock.advance(0.249)
    assert len(native.created) == 1
    clock.advance(0.001)
    assert len(requested) == 1 and requested[0] != first_endpoint
    assert controller._timer.isActive()
    assert len(native.created) == 2
    native.created[1].close.assert_not_called()


def test_constructor_failure_leaves_controller_retryable(surface, native, clock):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    endpoints = []
    surface.geometry_changed.connect(lambda: endpoints.append(controller.endpoint))
    factory = native.factory.side_effect
    native.factory.side_effect = RuntimeError("native allocation failed")
    with pytest.raises(RuntimeError, match="allocation failed"):
        _ = controller.endpoint
    assert not controller._timer.isActive()
    native.factory.side_effect = factory
    for _ in range(10):
        with pytest.raises(RuntimeError):
            _ = controller.endpoint
    native.factory.assert_called_once()
    clock.advance(0.250)
    assert endpoints == [controller.endpoint]
    assert len(native.created) == 1
    assert controller._timer.isActive()


@pytest.mark.parametrize("reset", ["close", "handle"])
def test_exhaustion_notifies_fallback_once_and_rejects_the_dead_endpoint(
    surface,
    native,
    clock,
    reset,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    factory = native.factory.side_effect

    def create(handle):
        presenter = factory(handle)
        presenter.poll.side_effect = RuntimeError("persistent context loss")
        return presenter

    native.factory.side_effect = create
    _ = controller.endpoint
    fallbacks = []

    def reconcile():
        try:
            _ = controller.endpoint
        except RuntimeError:
            fallbacks.append(True)
            surface.hide()
            controller.set_visible(False)

    surface.geometry_changed.connect(reconcile)
    clock.advance(20)
    assert fallbacks == [True]
    assert native.factory.call_count == 4
    assert not any(timer.isActive() for timer in clock.timers)
    # A geometry reconciliation also tries to show native output before reading
    # the endpoint. That automatic hide/show cycle must not renew an exhausted budget.
    for _ in range(5):
        surface.show()
        controller.set_visible(True)
        reconcile()
        clock.advance(20)
    assert native.factory.call_count == 4
    assert len(fallbacks) == 6

    native.factory.side_effect = factory
    surface.show()
    controller.set_visible(True)
    if reset == "close":
        controller.close()
    else:
        surface.address = 200
    assert controller.endpoint == native.created[-1].endpoint
    assert native.factory.call_count == 5
    assert controller._timer.isActive()


@pytest.mark.parametrize("dispose", [False, True])
def test_native_close_failure_does_not_abort_recovery_or_teardown(
    surface,
    native,
    clock,
    dispose,
):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    original = controller.endpoint
    old = native.created[0]
    old.poll.side_effect = RuntimeError("context loss")
    old.close.side_effect = RuntimeError("native teardown failed")
    controller._poll()
    clock.advance(0.250)
    assert controller.endpoint != original
    old.close.assert_called_once()

    replacement = native.created[1]
    replacement.close.side_effect = RuntimeError("native teardown failed again")
    if dispose:
        surface.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(controller)
    else:
        controller.close()
        controller.close()
    clock.advance(60)
    replacement.close.assert_called_once()
    assert not any(timer.isActive() for timer in clock.timers)


def test_disposal_stops_timer_and_releases_presenter_once(surface, native):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    _ = controller.endpoint
    presenter = native.created[0]
    surface.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(controller)
    presenter.close.assert_called_once()
    count = presenter.poll.call_count
    QTest.qWait(20)
    assert presenter.poll.call_count == count


def test_explicit_close_is_idempotent_and_allows_new_endpoint(surface, native):
    surface.show()
    controller = CocoaSurfacePresenterController(surface)
    old_endpoint = controller.endpoint
    controller.close()
    controller.close()
    assert not controller._timer.isActive()
    native.created[0].close.assert_called_once()
    assert controller.endpoint != old_endpoint
    assert controller._timer.isActive()


@pytest.mark.parametrize("exhausted", [False, True])
def test_real_surface_fallback_does_not_schedule_reconciliation_loop(
    monkeypatch,
    request,
    native,
    exhausted,
):
    import solin.main_window as main_window
    from solin.widgets.projection.native_surface import NativeVideoSurface

    surface = NativeVideoSurface()
    request.addfinalizer(surface.deleteLater)
    controller = CocoaSurfacePresenterController(surface)
    surface._cocoa_presenter = controller
    # Exercise the Cocoa route with real Qt widgets/timers on an offscreen host.
    monkeypatch.setattr(
        NativeVideoSurface,
        "surface_endpoint",
        property(lambda self: self._cocoa_presenter.endpoint),
    )
    monkeypatch.setattr(main_window, "_native_scene_routing_supported", lambda: True)
    native.factory.side_effect = RuntimeError("native allocation failed")
    if exhausted:
        monkeypatch.setattr(controller, "_RETRY_DELAYS_MS", (1, 1, 1))
        surface.show()
        with pytest.raises(RuntimeError):
            _ = surface.surface_endpoint
        retried = QSignalSpy(controller._retry_timer.timeout)
        while native.factory.call_count < 4:
            assert retried.wait(1000), "Recovery did not reach its terminal state"
        surface.hide()

    window = SimpleNamespace(native_output_active=False, native_video_surface=surface)

    def set_native_active(active):
        if window.native_output_active != active:
            window.native_output_active = active
            surface.setVisible(active)

    window.set_native_output_active = set_native_active
    host = SimpleNamespace(
        _native_window_output_suppressed=False,
        _geometry_watched_surfaces=[],
        projection_session=SimpleNamespace(
            state={"type": "video", "is_audio": False},
            projection_windows=(window,),
            all_windows=lambda: (window,),
        ),
        scene_runtime=SimpleNamespace(native_window_routing_ready=True, set_window_targets=Mock()),
        _projection_targets=SimpleNamespace(restore_state_to_window=Mock()),
        notifications=Mock(),
        tr=lambda value: value,
    )
    host._reconcile_native_scene_surfaces = Mock(
        side_effect=lambda: main_window.MainWindow._reconcile_native_scene_surfaces(host),
    )
    host._watch_surface_geometry = lambda surface: main_window.MainWindow._watch_surface_geometry(
        host,
        surface,
    )
    host._native_window_target = lambda surface, target_id, bus_id: (
        main_window.MainWindow._native_window_target(host, surface, target_id, bus_id)
    )
    host._reconcile_native_scene_surfaces()
    QTest.qWait(250)

    host._reconcile_native_scene_surfaces.assert_called_once()
    host.notifications.error.assert_called_once()
    host._projection_targets.restore_state_to_window.assert_called_once_with(window)
    host.scene_runtime.set_window_targets.assert_called_once_with(())
    assert not surface.isVisible()
    assert not surface._geometry_timer.isActive()
    assert not controller._timer.isActive()
    assert not controller._retry_timer.isActive()
    assert native.factory.call_count == (4 if exhausted else 1)
