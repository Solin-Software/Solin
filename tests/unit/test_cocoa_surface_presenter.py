"""Qt presenter ownership, timer, replacement and native-failure recovery."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, QThread, Signal
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


def test_poll_failure_closes_native_view_before_requesting_new_endpoint(surface, native):
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
    assert len(requested) == 1 and requested[0] != first_endpoint
    assert controller._timer.isActive()
    assert len(native.created) == 2
    native.created[1].close.assert_not_called()


def test_constructor_failure_leaves_controller_retryable(surface, native):
    controller = CocoaSurfacePresenterController(surface)
    factory = native.factory.side_effect
    native.factory.side_effect = RuntimeError("native allocation failed")
    with pytest.raises(RuntimeError, match="allocation failed"):
        _ = controller.endpoint
    assert not controller._timer.isActive()
    native.factory.side_effect = factory
    assert controller.endpoint
    assert len(native.created) == 1


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
