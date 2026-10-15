import threading
from types import SimpleNamespace

import pylibobs._ffi as pylibobs_ffi
import pytest

from solin.core.media.obs_frame_source import ObsFrameSource, _frame_ffi, create_frame_source


@pytest.fixture
def uploaded_frames(monkeypatch):
    uploads = []

    def copy_frame(_source, frame):
        if frame != _frame_ffi.NULL:
            uploads.append({
                "flags": int(frame.flags),
                "pixels": b"".join(
                    bytes(_frame_ffi.buffer(
                        frame.data[0] + row * frame.linesize[0], frame.width * 4,
                    ))
                    for row in range(frame.height)
                ),
            })

    lib = SimpleNamespace(
        obs_source_output_video=copy_frame,
        obs_source_preload_video=copy_frame,
        obs_source_show_preloaded_video=lambda _source: None,
    )
    monkeypatch.setattr(pylibobs_ffi, "ffi", _frame_ffi)
    monkeypatch.setattr(pylibobs_ffi, "get_lib", lambda: lib)
    monkeypatch.setattr(pylibobs_ffi, "is_alive", lambda: True)
    return ObsFrameSource(
        SimpleNamespace(_ptr="source"), upload_first_frame=lambda upload: upload(),
    ), uploads


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize(
    ("data", "width", "height", "stride", "expected_flags", "pixels"),
    [
        (b"\x20\x40\xe0\xff" * 2, 2, 1, 8, 1, b"\x20\x40\xe0\xff" * 2),
        (b"\x20\x40\xe0\x80" * 2, 2, 1, 8, 0, b"\x20\x40\xe0\x80" * 2),
        (b"\xff\xff\xff\x00", 1, 1, 4, 0, b"\xff\xff\xff\x00"),
        (b"\xff\xff\xff\xff\xff\xff\xff\xfe", 2, 1, 8, 0,
         b"\xff\xff\xff\xff\xff\xff\xff\xfe"),
        (b"\x20\x40\xe0\xff\x00\x20\x40\xe0\xff", 1, 2, 5, 1,
         b"\x20\x40\xe0\xff" * 2),
        (b"\x20\x40\xe0\xff\x00\x20\x40\xe0\x80", 1, 2, 5, 0,
         b"\x20\x40\xe0\xff\x20\x40\xe0\x80"),
        (b"\x20\x40\xe0\xff\x00\x00\x00\x00", 1, 1, 4, 1,
         b"\x20\x40\xe0\xff"),
    ],
    ids=["opaque", "translucent", "transparent", "last-pixel-alpha",
         "padded-rows", "padded-row-alpha", "trailing-capacity"],
)
def test_upload_declares_linear_alpha_only_for_fully_opaque_pixels(
    uploaded_frames, reset, data, width, height, stride, expected_flags, pixels,
):
    source, uploads = uploaded_frames
    assert source.push_bgra(data, width, height, stride, reset=reset)
    assert uploads == [{"flags": expected_flags, "pixels": pixels}]


@pytest.mark.parametrize("reset", [False, True])
def test_upload_updates_alpha_semantics_for_every_frame(uploaded_frames, reset):
    source, uploads = uploaded_frames
    for alpha in (255, 128, 255):
        assert source.push_bgra(bytes((10, 20, 30, alpha)), 1, 1, 4, reset=reset)
    assert [frame["flags"] for frame in uploads] == [1, 0, 1]


@pytest.mark.parametrize("buffer_type", [bytes, bytearray, memoryview])
def test_upload_accepts_contiguous_bgra_buffers(uploaded_frames, buffer_type):
    source, uploads = uploaded_frames
    pixels = bytes((32, 64, 224, 255))
    assert source.push_bgra(buffer_type(pixels), 1, 1, 4)
    assert uploads == [{"flags": 1, "pixels": pixels}]


@pytest.mark.parametrize(
    ("data", "width", "height", "stride"),
    [(b"", 1, 1, 4), (b"\xff" * 3, 1, 1, 4), (b"\xff" * 7, 1, 2, 4),
     (b"\xff" * 12, 2, 2, 8), (b"\xff" * 4, 1, 1, 3),
     (b"\xff" * 4, 1, 1, -4), (b"\xff" * 4, 0, 1, 4),
     (b"\xff" * 4, 1, 0, 4)],
)
def test_upload_rejects_incomplete_bgra_layout_before_native_calls(
    uploaded_frames, data, width, height, stride,
):
    source, uploads = uploaded_frames
    assert not source.push_bgra(data, width, height, stride)
    assert uploads == []


@pytest.fixture
def queued_frame_source(monkeypatch):
    cancelled = threading.Event()
    registered = threading.Event()
    callbacks = []
    removed = []
    calls = []
    native_source = SimpleNamespace(_ptr="source", release=lambda: None)

    def register(callback):
        callbacks.append(callback)
        registered.set()
        return callback

    runtime = SimpleNamespace(ob=SimpleNamespace(
        Source=SimpleNamespace(create=lambda *_args: native_source),
        add_main_render_callback=register,
        remove_main_render_callback=removed.append,
    ))
    lib = SimpleNamespace(
        obs_source_get_output_flags=lambda _source: 5,
        obs_source_set_async_unbuffered=lambda *_args: None,
        obs_source_output_video=lambda _source, frame: calls.append((
            "clear" if frame == _frame_ffi.NULL else "publish", threading.get_ident(),
        )),
        obs_source_preload_video=lambda *_args: calls.append(("preload", threading.get_ident())),
        obs_source_show_preloaded_video=lambda *_args: calls.append(("show", threading.get_ident())),
    )
    monkeypatch.setattr(pylibobs_ffi, "ffi", _frame_ffi)
    monkeypatch.setattr(pylibobs_ffi, "get_lib", lambda: lib)
    monkeypatch.setattr(pylibobs_ffi, "is_alive", lambda: True)
    source = create_frame_source(runtime, cancelled=cancelled)
    assert source is not None
    yield SimpleNamespace(
        source=source, cancelled=cancelled, registered=registered, callbacks=callbacks,
        removed=removed, calls=calls, lib=lib, runtime=runtime,
    )
    cancelled.set()
    source.release()


def _first_frame_worker(state):
    results = []
    worker = threading.Thread(target=lambda: results.append(
        state.source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4, reset=True),
    ))
    worker.start()
    return worker, results


def test_first_frame_waits_for_upload_on_the_native_render_thread(queued_frame_source):
    state = queued_frame_source
    worker, results = _first_frame_worker(state)
    try:
        assert state.registered.wait(2), "First-frame upload bypassed the native render queue"
        assert results == []
        assert state.calls == [("clear", worker.ident), ("preload", worker.ident)]
        # Model OBS invoking the callback with its graphics context already held.
        state.callbacks[0](1920, 1080)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [True]
        assert state.calls[-1] == ("show", threading.get_ident())
        # Another video tick before unregistration must not replay the upload.
        state.callbacks[0](1920, 1080)
        assert [action for action, _thread in state.calls] == ["clear", "preload", "show"]
        assert state.removed == state.callbacks
    finally:
        state.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_first_frame_waiter_drains_native_callback_removal(queued_frame_source):
    state = queued_frame_source
    removing, release_removal = threading.Event(), threading.Event()

    def remove(callback):
        removing.set()
        # OBS removal acquires its draw-callback lock after upload has completed.
        assert release_removal.wait(2), "Test did not drain native callback removal"
        state.removed.append(callback)

    state.runtime.ob.remove_main_render_callback = remove
    worker, results = _first_frame_worker(state)
    try:
        assert state.registered.wait(2)
        state.callbacks[0](1920, 1080)
        assert removing.wait(2)
        assert worker.is_alive() and results == []
        assert state.source.source is not None
        release_removal.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [True] and state.removed == state.callbacks
    finally:
        release_removal.set()
        state.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_pending_first_frame_is_cancelled_before_source_teardown(queued_frame_source):
    state = queued_frame_source
    worker, results = _first_frame_worker(state)
    try:
        assert state.registered.wait(2)
        state.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [False]
        assert state.removed == state.callbacks
        state.callbacks[0](1920, 1080)
        assert [action for action, _thread in state.calls] == ["clear", "preload"]
    finally:
        state.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_running_first_frame_finishes_before_cancelled_owner_is_released(queued_frame_source):
    state = queued_frame_source
    entered = threading.Event()
    finish = threading.Event()

    def upload(_source):
        entered.set()
        assert finish.wait(2), "Test did not release the running native upload"

    state.lib.obs_source_show_preloaded_video = upload
    worker, results = _first_frame_worker(state)
    renderer = None
    try:
        assert state.registered.wait(2)
        renderer = threading.Thread(target=lambda: state.callbacks[0](1920, 1080))
        renderer.start()
        assert entered.wait(2)
        state.cancelled.set()
        assert worker.is_alive()
        assert results == []
        assert state.removed == []
        assert state.source.source is not None
        finish.set()
        renderer.join(timeout=2)
        worker.join(timeout=2)
        assert not renderer.is_alive() and not worker.is_alive()
        assert results == [True]
        assert state.removed == state.callbacks
    finally:
        finish.set()
        state.cancelled.set()
        if renderer is not None:
            renderer.join(timeout=2)
            assert not renderer.is_alive()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("error", [RuntimeError("upload rejected"), TimeoutError("native timeout")])
def test_native_first_frame_error_retires_its_callback(queued_frame_source, error):
    state = queued_frame_source

    def fail(_source):
        raise error

    state.lib.obs_source_show_preloaded_video = fail
    worker, results = _first_frame_worker(state)
    try:
        assert state.registered.wait(2)
        state.callbacks[0](1920, 1080)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [False]
        assert state.removed == state.callbacks
    finally:
        state.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_async_frames_do_not_queue_a_first_frame_upload(queued_frame_source):
    state = queued_frame_source
    assert state.source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4)
    assert [action for action, _thread in state.calls] == ["publish"]
    assert state.callbacks == []


def test_already_cancelled_first_frame_does_not_register_a_callback(queued_frame_source):
    state = queued_frame_source
    state.cancelled.set()
    assert not state.source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4, reset=True)
    assert state.callbacks == []
    assert [action for action, _thread in state.calls] == ["clear", "preload"]


def test_first_frame_registration_failure_is_not_accepted(queued_frame_source):
    state = queued_frame_source

    def fail_registration(_callback):
        raise RuntimeError("native render callback unavailable")

    state.runtime.ob.add_main_render_callback = fail_registration
    assert not state.source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4, reset=True)
    assert state.callbacks == []
    assert state.removed == []


@pytest.mark.parametrize("reason", ["stale_revision", "deadline_exceeded"])
def test_waiting_upload_checks_generation_and_deadline_without_a_render_tick(
    queued_frame_source, reason,
):
    state = queued_frame_source
    invalidated = threading.Event()
    checked = threading.Event()

    def check_current():
        checked.set()
        if invalidated.is_set():
            raise RuntimeError(reason)

    source = create_frame_source(
        state.runtime, cancelled=state.cancelled, check_current=check_current,
    )
    assert source is not None
    results = []
    worker = threading.Thread(target=lambda: results.append(
        source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4, reset=True),
    ))
    try:
        worker.start()
        assert state.registered.wait(2)
        assert checked.wait(2)
        invalidated.set()
        worker.join(2)
        assert not worker.is_alive(), "Stale upload still waits for a missing render callback"
        assert results == [False] and state.removed == state.callbacks
        state.callbacks[0](1920, 1080)
        assert "show" not in [action for action, _thread in state.calls]
    finally:
        invalidated.set()
        state.cancelled.set()
        worker.join(2)
        source.release()


def test_stale_running_upload_drains_before_releasing_its_native_source(queued_frame_source):
    state = queued_frame_source
    entered, finish = threading.Event(), threading.Event()
    invalidated, checked = threading.Event(), threading.Event()

    def check_current():
        if invalidated.is_set():
            checked.set()
            raise RuntimeError("stale_revision")

    def upload(_source):
        entered.set()
        assert finish.wait(2), "Native upload was not released"

    state.lib.obs_source_show_preloaded_video = upload
    source = create_frame_source(
        state.runtime, cancelled=state.cancelled, check_current=check_current,
    )
    assert source is not None
    results = []
    worker = threading.Thread(target=lambda: results.append(
        source.push_bgra(b"\x20\x40\xe0\xff", 1, 1, 4, reset=True),
    ))
    renderer = None
    try:
        worker.start()
        assert state.registered.wait(2)
        renderer = threading.Thread(target=lambda: state.callbacks[0](1920, 1080))
        renderer.start()
        assert entered.wait(2)
        invalidated.set()
        assert checked.wait(2)
        assert worker.is_alive() and state.removed == []
        assert source.source is not None
        finish.set()
        renderer.join(2)
        worker.join(2)
        assert not renderer.is_alive() and not worker.is_alive()
        assert results == [False] and state.removed == state.callbacks
    finally:
        finish.set()
        state.cancelled.set()
        if renderer is not None:
            renderer.join(2)
        worker.join(2)
        source.release()


@pytest.mark.parametrize("rendered", [False, True])
def test_failed_callback_removal_keeps_source_and_handle_for_release_retry(
    queued_frame_source, rendered,
):
    state = queued_frame_source
    released = []
    state.source.source.release = lambda: released.append("source")
    attempts = []
    fail_removal = True

    def remove(callback):
        attempts.append(callback)
        if fail_removal:
            raise RuntimeError("native removal failed")
        state.removed.append(callback)

    state.runtime.ob.remove_main_render_callback = remove
    worker, results = _first_frame_worker(state)
    try:
        assert state.registered.wait(2)
        if rendered:
            state.callbacks[0](1920, 1080)
        else:
            state.cancelled.set()
        worker.join(2)
        assert not worker.is_alive()
        assert results == [False]
        with pytest.raises(RuntimeError, match="native removal failed"):
            state.source.release()
        assert state.source.source is not None and released == []
        state.callbacks[0](1920, 1080)
        assert [action for action, _thread in state.calls].count("show") == int(rendered)

        fail_removal = False
        state.source.release()
        state.source.release()
        assert released == ["source"]
        assert state.removed == state.callbacks
        assert attempts == state.callbacks * 3
    finally:
        state.cancelled.set()
        worker.join(2)
