"""Native readiness lifecycle tests without loading libobs or running a decoder."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from solin.core.media.obs_video_readiness import ObsSourceVideoReadiness


def _watch(*, results=(False, True)):
    source = SimpleNamespace(release=Mock(), update=Mock())
    callback_handle = object()
    runtime = SimpleNamespace(
        ob=SimpleNamespace(
            add_main_render_callback=Mock(return_value=callback_handle),
            remove_main_render_callback=Mock(),
        ),
        prime_source_video=Mock(side_effect=results),
        observe_source_updates=Mock(return_value=Mock()),
        discard_selected_source_video=Mock(),
    )
    observer = ObsSourceVideoReadiness(runtime, source)
    callback = runtime.ob.add_main_render_callback.call_args.args[0]
    return observer, runtime, source, callback, callback_handle


def test_observer_registers_before_priming_and_latches_the_first_uploaded_frame():
    observer, runtime, source, callback, _handle = _watch()
    try:
        assert not observer.ready
        runtime.ob.add_main_render_callback.assert_called_once_with(callback)
        runtime.prime_source_video.assert_not_called()
        callback(1920, 1080)
        assert not observer.ready
        callback(1920, 1080)
        assert observer.ready
        callback(1920, 1080)
        assert runtime.prime_source_video.call_args_list == [
            call(source), call(source),
        ]
    finally:
        observer.close()


@pytest.mark.parametrize("initially_ready", [False, True])
def test_invalidating_a_generation_requires_a_new_frame_without_cached_preload(initially_ready):
    observer, runtime, source, callback, _handle = _watch(results=())
    runtime.prime_source_video.side_effect = None
    runtime.prime_source_video.return_value = initially_ready
    try:
        callback(160, 90)
        assert observer.ready is initially_ready
        observer.invalidate()
        assert not observer.ready
        runtime.prime_source_video.return_value = False
        callback(160, 90)
        assert not observer.ready
        runtime.prime_source_video.return_value = True
        callback(160, 90)
        assert observer.ready
        callback(160, 90)
        assert runtime.prime_source_video.call_args_list == [
            call(source),
            call(source),
            call(source),
        ]
        observer.invalidate()
        assert not observer.ready
        callback(160, 90)
        assert observer.ready
        runtime.prime_source_video.assert_called_with(source)
    finally:
        observer.close()


def test_native_callback_failure_is_reported_once_and_latched(caplog):
    observer, runtime, source, callback, _handle = _watch(results=RuntimeError("upload failed"))
    try:
        callback(160, 90)  # An exception must not cross the native callback boundary.
        callback(160, 90)
        assert not observer.ready
        runtime.prime_source_video.assert_called_once_with(source)
        failures = [record for record in caplog.records if "Could not prepare" in record.message]
        assert len(failures) == 1
        assert isinstance(failures[0].exc_info[1], RuntimeError)
        # A new decoder generation can recover; this is not a retry of the
        # failed generation inside its preparation deadline.
        runtime.prime_source_video.side_effect = (True,)
        observer.invalidate()
        callback(160, 90)
        assert observer.ready
        runtime.prime_source_video.assert_called_with(source)
    finally:
        observer.close()


@pytest.mark.parametrize("ready_before_close", [False, True])
def test_close_unregisters_its_handle_once_and_never_releases_the_borrowed_source(ready_before_close):
    observer, runtime, source, callback, handle = _watch(results=(True,))
    if ready_before_close:
        callback(160, 90)
    previous_calls = runtime.prime_source_video.call_count
    observer.close()
    assert not observer.ready
    observer.close()
    observer.invalidate()
    callback(160, 90)  # A captured late callback must not touch the detached source.
    runtime.ob.remove_main_render_callback.assert_called_once_with(handle)
    assert runtime.prime_source_video.call_count == previous_calls
    source.release.assert_not_called()
    runtime.observe_source_updates.return_value.assert_called_once_with()


def test_failed_callback_registration_propagates_without_taking_source_ownership():
    source = SimpleNamespace(release=Mock(), update=Mock())
    disconnect = Mock()
    runtime = SimpleNamespace(
        ob=SimpleNamespace(
            add_main_render_callback=Mock(side_effect=RuntimeError("registration failed")),
            remove_main_render_callback=Mock(),
        ),
        observe_source_updates=Mock(return_value=disconnect),
    )
    with pytest.raises(RuntimeError, match="registration failed"):
        ObsSourceVideoReadiness(runtime, source)
    source.release.assert_not_called()
    runtime.ob.remove_main_render_callback.assert_not_called()
    disconnect.assert_called_once_with()


def test_close_does_not_hold_the_callback_lock_while_native_unregistration_drains_callbacks():
    observer, runtime, source, callback, handle = _watch()
    completed = threading.Event()
    errors = []

    def unregister(selected):
        assert selected is handle
        callback(160, 90)  # Model native removal draining an already queued callback.

    def close():
        try:
            observer.close()
        except Exception as error:  # noqa: BLE001 - propagate worker failures to the test
            errors.append(error)
        finally:
            completed.set()

    runtime.ob.remove_main_render_callback.side_effect = unregister
    worker = threading.Thread(target=close, daemon=True)
    worker.start()
    assert completed.wait(5), "Unregistration deadlocked against the callback lock"
    worker.join(timeout=5)
    assert not worker.is_alive() and errors == []
    runtime.prime_source_video.assert_not_called()
    source.release.assert_not_called()
    runtime.ob.remove_main_render_callback.assert_called_once_with(handle)


def test_concurrent_callbacks_upload_only_once_for_one_generation():
    observer, runtime, source, callback, _handle = _watch(results=(True,))
    start = threading.Barrier(3, timeout=5)
    errors = []

    def render():
        try:
            start.wait()
            callback(160, 90)
        except Exception as error:  # noqa: BLE001 - propagate worker failures to the test
            errors.append(error)

    workers = [threading.Thread(target=render, daemon=True) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=5)
        assert all(not worker.is_alive() for worker in workers)
        assert errors == [] and observer.ready
        runtime.prime_source_video.assert_called_once_with(source)
    finally:
        observer.close()


def test_deferred_update_cannot_accept_the_preceding_ticks_frame():
    observer, runtime, source, callback, _handle = _watch(results=(True, True))
    updated = runtime.observe_source_updates.call_args.args[1]
    try:
        callback(64, 64)
        assert observer.ready
        observer.update({"speed_percent": 150})
        assert observer.updating and not observer.ready
        source.update.assert_called_once_with({"speed_percent": 150})
        callback(64, 64)
        assert runtime.prime_source_video.call_count == 1
        updated()
        runtime.discard_selected_source_video.assert_called_once_with(source)
        assert not observer.updating and not observer.ready
        callback(64, 64)
        assert observer.ready
    finally:
        observer.close()


def test_rejected_update_fails_generation_without_stranding_the_update_gate():
    observer, runtime, source, callback, _handle = _watch(results=(True, True))
    updated = runtime.observe_source_updates.call_args.args[1]
    try:
        callback(64, 64)
        source.update.side_effect = RuntimeError("queue rejected")
        with pytest.raises(RuntimeError, match="queue rejected"):
            observer.update({"speed_percent": 150})
        assert not observer.updating and not observer.ready
        callback(64, 64)
        assert runtime.prime_source_video.call_count == 1
        source.update.side_effect = None
        observer.update({"speed_percent": 125})
        updated()
        callback(64, 64)
        assert observer.ready
    finally:
        observer.close()


def test_update_acknowledgement_failure_never_primes_an_old_frame(caplog):
    observer, runtime, source, callback, _handle = _watch(results=(True,))
    updated = runtime.observe_source_updates.call_args.args[1]
    try:
        observer.update({"input": "video"})
        runtime.discard_selected_source_video.side_effect = RuntimeError("release failed")
        updated()
        callback(64, 64)
        assert not observer.ready and not observer.updating
        runtime.prime_source_video.assert_not_called()
        assert "Could not acknowledge" in caplog.text
    finally:
        observer.close()


def test_close_disconnects_update_signal_even_if_render_removal_raises():
    observer, runtime, source, callback, handle = _watch()
    runtime.ob.remove_main_render_callback.side_effect = RuntimeError("removal failed")
    with pytest.raises(RuntimeError, match="removal failed"):
        observer.close()
    runtime.observe_source_updates.return_value.assert_called_once_with()
    runtime.ob.remove_main_render_callback.side_effect = None
    observer.close()
    callback(64, 64)
    assert runtime.ob.remove_main_render_callback.call_args_list == [call(handle), call(handle)]
    runtime.prime_source_video.assert_not_called()
    source.release.assert_not_called()


def test_failed_update_signal_registration_does_not_register_render_or_release_source():
    source = SimpleNamespace(release=Mock())
    runtime = SimpleNamespace(
        observe_source_updates=Mock(side_effect=RuntimeError("signal failed")),
        ob=SimpleNamespace(add_main_render_callback=Mock()),
    )
    with pytest.raises(RuntimeError, match="signal failed"):
        ObsSourceVideoReadiness(runtime, source)
    runtime.ob.add_main_render_callback.assert_not_called()
    source.release.assert_not_called()


def test_failed_signal_removal_retains_trampoline_for_an_explicit_cleanup_retry():
    observer, runtime, source, callback, handle = _watch()
    disconnect = runtime.observe_source_updates.return_value
    disconnect.side_effect = RuntimeError("disconnect failed")
    with pytest.raises(RuntimeError, match="disconnect failed"):
        observer.close()
    callback(64, 64)
    runtime.prime_source_video.assert_not_called()
    runtime.ob.remove_main_render_callback.assert_called_once_with(handle)
    assert observer._disconnect_update is disconnect
    disconnect.side_effect = None
    observer.close()
    assert disconnect.call_count == 2 and observer._disconnect_update is None
    source.release.assert_not_called()
