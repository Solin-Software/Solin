"""Projection graph routing and surface lifecycle, without a native renderer."""
from __future__ import annotations

import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.scenes.libobs_surface_output import LibobsSurfaceOutput
from solin.core.scenes.libobs_window_output import LibobsWindowOutput


def _target(endpoint="one", **fields):
    return {"surface_endpoint": "org.pylibobs.surfaces." + endpoint,
            "target_id": endpoint, "bus_id": "media_windows", **fields}


@pytest.fixture
def backend(monkeypatch):
    producers = []
    callbacks = {}

    def create(endpoint, width, height):
        producer = Mock()
        producer.endpoint = endpoint
        producer.render.return_value = True
        producers.append(producer)
        return producer

    def add(callback):
        token = object()
        callbacks[token] = callback
        return token

    def remove(token):
        callbacks.pop(token)

    native = ModuleType("pylibobs.macos_surfaces")
    native.SurfaceProducer = Mock(side_effect=create)
    monkeypatch.setitem(sys.modules, native.__name__, native)
    runtime = SimpleNamespace(video=SimpleNamespace(width=1280, height=720, fps=30),
                              ob=SimpleNamespace(add_main_render_callback=Mock(side_effect=add),
                                                 remove_main_render_callback=Mock(side_effect=remove)))
    return SimpleNamespace(runtime=runtime, producers=producers, callbacks=callbacks, factory=native.SurfaceProducer)


def _manager(backend, resolver=None):
    return LibobsSurfaceOutput(backend.runtime, threading.RLock(), resolver or Mock(return_value=object()))


def test_projection_and_editor_use_their_own_graph_sources(backend):
    projection, editor = object(), object()
    native_factory = Mock(side_effect=AssertionError("surface targets must not create native displays"))
    manager = LibobsWindowOutput(backend.runtime, native_factory)
    projection_resolver = Mock(return_value=projection)
    scene_resolver = Mock(return_value=editor)
    manager.set_projection_resolver(projection_resolver)
    manager.set_scene_resolver(scene_resolver)
    manager.set_targets([_target(), _target("editor", scene_id="editing-scene")])
    next(iter(backend.callbacks.values()))()
    backend.producers[0].render.assert_called_once_with(projection, canvas_width=1280, canvas_height=720)
    backend.producers[1].render.assert_called_once_with(editor, canvas_width=1280, canvas_height=720)
    scene_resolver.assert_called_once_with("editing-scene")
    assert manager.render_targets == (("projection", ""), ("scene", "editing-scene"))
    native_factory.assert_not_called()
    manager.shutdown()


def test_missing_projection_emits_black_frame_without_falling_back_to_program(backend):
    manager = LibobsWindowOutput(backend.runtime, Mock())
    manager.set_targets([_target()])
    next(iter(backend.callbacks.values()))()
    backend.producers[0].render.assert_called_once_with(None, canvas_width=1280, canvas_height=720)
    manager.shutdown()


def test_explicit_program_surface_route_is_rejected(backend):
    manager = _manager(backend)
    with pytest.raises(ValueError, match="projection"):
        manager.set_targets([_target(bus_id="virtual_camera")])
    backend.factory.assert_not_called()
    assert not backend.callbacks


def test_graph_contention_skips_frame_without_blocking_graphics_thread(backend):
    guard = threading.RLock()
    resolver = Mock()
    manager = LibobsSurfaceOutput(backend.runtime, guard, resolver)
    manager.set_targets([_target()])
    callback = next(iter(backend.callbacks.values()))
    with guard:
        worker = threading.Thread(target=callback)
        worker.start()
        worker.join(timeout=1)
        completed_without_guard = not worker.is_alive()
    worker.join(timeout=1)
    assert completed_without_guard
    resolver.assert_not_called()
    backend.producers[0].render.assert_not_called()
    manager.shutdown()


def test_reconcile_callback_removal_cannot_deadlock_on_graph_guard(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    original_remove = backend.runtime.ob.remove_main_render_callback.side_effect

    def synchronize(token):
        # Model native removal waiting for an already-dispatched gfx callback.
        worker = threading.Thread(target=backend.callbacks[token])
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive(), "render callback waited for the reconcile guard"
        original_remove(token)

    backend.runtime.ob.remove_main_render_callback.side_effect = synchronize
    manager.set_targets([])
    backend.producers[0].render.assert_not_called()
    backend.producers[0].close.assert_called_once()
    assert not backend.callbacks


def test_slow_consumer_does_not_prevent_other_targets_rendering(backend):
    manager = _manager(backend)
    manager.set_targets([_target(), _target("two")])
    backend.producers[0].render.return_value = False
    next(iter(backend.callbacks.values()))()
    assert all(producer.render.call_count == 1 for producer in backend.producers)
    manager.shutdown()


def test_disconnected_consumer_does_not_prevent_other_targets_rendering(backend, caplog):
    manager = _manager(backend)
    manager.set_targets([_target(), _target("two")])
    backend.producers[0].render.side_effect = ConnectionError("receiver exited")
    next(iter(backend.callbacks.values()))()
    backend.producers[1].render.assert_called_once()
    assert not caplog.records
    assert len(manager.render_targets) == 1
    manager.shutdown()


def test_closed_receiver_is_retired_once_before_control_thread_cleanup(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    producer = backend.producers[0]
    producer.render.side_effect = ConnectionError("receiver exited")
    callback = next(iter(backend.callbacks.values()))

    callback()
    callback()

    assert producer.render.call_count == 1
    assert not manager.render_targets
    producer.close.assert_not_called()
    backend.runtime.ob.remove_main_render_callback.assert_not_called()
    manager.shutdown()


def test_control_poll_closes_retired_owner_and_removes_last_callback(backend, caplog):
    manager = _manager(backend)
    manager.set_targets([_target()])
    producer = backend.producers[0]
    producer.render.side_effect = ConnectionError("window closed")
    next(iter(backend.callbacks.values()))()

    manager.poll()
    manager.poll()
    manager.shutdown()

    producer.close.assert_called_once()
    assert not backend.callbacks
    assert not caplog.records


def test_unexpected_native_failure_is_reported_once_and_retired(backend, caplog):
    manager = _manager(backend)
    manager.set_targets([_target()])
    producer = backend.producers[0]
    producer.render.side_effect = RuntimeError("native render failed")
    callback = next(iter(backend.callbacks.values()))
    callback()
    callback()
    manager.poll()

    assert producer.render.call_count == 1
    producer.close.assert_called_once()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert caplog.records[0].exc_info is not None
    manager.shutdown()


def test_poll_synchronizes_callback_without_blocking_the_graphics_thread(backend):
    manager = _manager(backend)
    manager.set_targets([_target(), _target("healthy")])
    backend.producers[0].render.side_effect = ConnectionError("window closed")
    next(iter(backend.callbacks.values()))()
    original_remove = backend.runtime.ob.remove_main_render_callback.side_effect

    def synchronize(token):
        worker = threading.Thread(target=backend.callbacks[token])
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive(), "control cleanup blocked the graphics callback"
        original_remove(token)

    backend.runtime.ob.remove_main_render_callback.side_effect = synchronize
    manager.poll()
    next(iter(backend.callbacks.values()))()

    backend.producers[0].close.assert_called_once()
    assert backend.producers[0].render.call_count == 1
    assert backend.producers[1].render.call_count == 2
    assert len(manager.render_targets) == 1
    manager.shutdown()


def test_failed_identity_waits_for_withdrawal_or_replacement(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    backend.producers[0].render.side_effect = ConnectionError("window closed")
    next(iter(backend.callbacks.values()))()

    manager.set_targets([_target()])
    assert len(backend.producers) == 1 and not backend.callbacks
    manager.set_targets([_target("replacement")])
    assert len(backend.producers) == 2 and len(backend.callbacks) == 1
    manager.set_targets([])
    manager.set_targets([_target()])
    assert len(backend.producers) == 3
    manager.shutdown()
    assert all(producer.close.call_count == 1 for producer in backend.producers)


def test_shutdown_and_late_poll_do_not_repeat_native_cleanup(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    producer = backend.producers[0]
    manager.shutdown()
    manager.poll()
    manager.shutdown()
    producer.close.assert_called_once()
    assert not backend.callbacks


def test_failed_scene_resolution_is_isolated_to_its_target(backend):
    resolver = Mock(side_effect=[LookupError("scene was removed"), object()])
    manager = _manager(backend, resolver)
    manager.set_targets([_target(), _target("two")])
    next(iter(backend.callbacks.values()))()
    backend.producers[0].render.assert_not_called()
    backend.producers[1].render.assert_called_once()
    manager.shutdown()


def test_route_update_reuses_producer_but_re_resolves_scene(backend):
    resolver = Mock()
    manager = _manager(backend, resolver)
    manager.set_targets([_target(scene_id="first")])
    manager.set_targets([_target(scene_id="second")])
    next(iter(backend.callbacks.values()))()
    assert len(backend.producers) == 1
    assert resolver.call_args.args[0]["scene_id"] == "second"
    assert len(backend.callbacks) == 1
    manager.shutdown()


def test_endpoint_replacement_and_canvas_resize_recreate_owned_producers(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    manager.set_targets([_target("replacement")])
    backend.producers[0].close.assert_called_once()
    backend.runtime.video.width = 1920
    backend.runtime.video.height = 1080
    manager.set_targets([_target("replacement")])
    backend.producers[1].close.assert_called_once()
    assert len(backend.producers) == 3
    backend.factory.assert_called_with("org.pylibobs.surfaces.replacement", 1920, 1080)
    manager.shutdown()


def test_disabled_targets_release_resources_and_callbacks_then_can_reenable(backend):
    manager = _manager(backend)
    manager.set_targets([_target()])
    manager.set_targets([_target(visible=False)])
    backend.producers[0].close.assert_called_once()
    assert not backend.callbacks and not manager.render_targets
    manager.set_targets([_target()])
    assert len(backend.producers) == 2 and len(backend.callbacks) == 1
    manager.shutdown()
    manager.shutdown()
    backend.producers[1].close.assert_called_once()
    assert not backend.callbacks


def test_surface_target_serializes_connection_metadata_and_zero_native_handle():
    from solin.core.scenes.engine import OutputWindowTarget
    from solin.core.scenes.model import BusId
    from solin.core.scenes.process_engine import _window_target_record

    values = dict(bus_id=BusId.MEDIA_WINDOWS, target_id="projection", screen_id="screen",
                  native_handle=0, surface_endpoint="org.pylibobs.surfaces.example",
                  x=0, y=0, width=640, height=360, device_pixel_ratio=2)
    target = OutputWindowTarget(**values)
    record = _window_target_record(target)
    assert record["native_handle"] == 0
    assert record["surface_endpoint"] == target.surface_endpoint
    with pytest.raises(ValueError, match="no native handle"):
        OutputWindowTarget(**{**values, "native_handle": 0x123456})


def test_unavailable_endpoint_does_not_prevent_healthy_window_connection(backend):
    manager = _manager(backend)
    create = backend.factory.side_effect

    def connect(endpoint, width, height):
        if endpoint.endswith(".unavailable"):
            raise ConnectionError("receiver closed before request arrived")
        return create(endpoint, width, height)

    backend.factory.side_effect = connect
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.set_targets([_target("unavailable"), _target("healthy")])
    assert len(backend.producers) == 1
    assert backend.producers[0].endpoint.endswith(".healthy")
    next(iter(backend.callbacks.values()))()
    backend.producers[0].render.assert_called_once()
    manager.shutdown()


def test_native_render_cadence_does_not_drop_frames_under_clock_jitter(backend, monkeypatch):
    import time
    ticks = iter(100 + i / 30 + (0.0001 if i % 2 else 0) for i in range(300))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    manager = _manager(backend)
    manager.set_targets([_target()])
    render = next(iter(backend.callbacks.values()))
    for _ in range(300):
        render()
    assert backend.producers[0].render.call_count == 300
    manager.shutdown()


def test_failed_connection_reports_error_and_can_retry_without_losing_healthy_target(backend):
    manager = _manager(backend)
    create = backend.factory.side_effect
    failed = True

    def connect(endpoint, width, height):
        if endpoint.endswith(".unavailable") and failed:
            raise RuntimeError("native allocation failed")
        return create(endpoint, width, height)

    backend.factory.side_effect = connect
    targets = [_target("unavailable"), _target("healthy")]
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.set_targets(targets)
    assert len(manager.render_targets) == 1
    healthy = backend.producers[0]
    failed = False
    manager.set_targets(targets)
    assert len(manager.render_targets) == 2
    next(iter(backend.callbacks.values()))()
    healthy.render.assert_called_once()
    assert backend.producers.count(healthy) == 1
    manager.shutdown()
