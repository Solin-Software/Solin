"""Preview rendering through libobs callbacks, without native graphics or shared memory."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.media import obs_source_render
from solin.core.scenes import content_frame_channel, libobs_preview_egress
from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA


class _Callbacks:
    def __init__(self, events):
        self.callbacks = {}
        self.events = events
        self.on_remove = None
        self.remove_requested = threading.Event()
        self._native_lock = threading.Lock()

    def add_main_render_callback(self, callback):
        token = object()
        self.callbacks[token] = callback
        self.events.append("register")
        return token

    def remove_main_render_callback(self, token):
        self.remove_requested.set()
        with self._native_lock:
            callback = self.callbacks[token]
            self.events.append("remove start")
            if self.on_remove is not None:
                self.on_remove(callback)
            del self.callbacks[token]
            self.events.append("remove end")

    def render(self, width=1920, height=1080):
        with self._native_lock:
            for callback in tuple(self.callbacks.values()):
                callback(width, height)


class _Writer:
    def __init__(self, width, height, *, name, create, events):
        self.descriptor = (width, height, name, create)
        self.frames = []
        self.close_count = 0
        self.events = events
        self.on_write = None

    def write(self, data, *, stride):
        assert self.close_count == 0, "Published to a closed writer"
        if self.on_write is not None:
            self.on_write()
        self.frames.append((bytes(data), stride))
        self.events.append("write")

    def close(self):
        self.close_count += 1
        self.events.append("close")


@pytest.fixture
def preview(monkeypatch):
    events = []
    callbacks = _Callbacks(events)
    writers = []

    def create_writer(width, height, *, name, create):
        writer = _Writer(width, height, name=name, create=create, events=events)
        writers.append(writer)
        return writer

    renderer = Mock(return_value=(bytes(range(16)), 8))
    resolve = Mock(return_value=renderer)
    monkeypatch.setattr(content_frame_channel, "SharedFrameChannelWriter", create_writer)
    monkeypatch.setattr(obs_source_render, "resolve_render_source_to_bgra", resolve)
    # Patch only this module's threading binding, not the test's coordination threads.
    worker = Mock(side_effect=AssertionError("Preview must use the native render callback"))
    monkeypatch.setattr(
        libobs_preview_egress,
        "threading",
        SimpleNamespace(
            Lock=threading.Lock,
            RLock=threading.RLock,
            Event=threading.Event,
            Thread=worker,
            current_thread=threading.current_thread,
        ),
    )
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        libobs_preview_egress,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
        ),
        raising=False,
    )
    before_render = Mock(return_value=True)
    runtime = SimpleNamespace(ob=callbacks, video=SimpleNamespace(width=1920, height=1080))
    egress = libobs_preview_egress.LibobsPreviewEgress(runtime, before_render=before_render)
    harness = SimpleNamespace(
        egress=egress,
        callbacks=callbacks,
        writers=writers,
        events=events,
        renderer=renderer,
        resolve=resolve,
        before_render=before_render,
        worker=worker,
        clock=clock,
        descriptor={
            "transport": SHARED_MEMORY_BGRA,
            "handle_token": "preview",
            "width": 2,
            "height": 2,
        },
    )
    try:
        yield harness
    finally:
        egress.shutdown()


def test_valid_descriptor_registers_native_callback_without_starting_a_worker(preview):
    preview.egress.configure(preview.descriptor)

    assert len(preview.callbacks.callbacks) == 1
    assert preview.writers[0].descriptor == (2, 2, "preview", False)
    preview.worker.assert_not_called()


def test_disabled_missing_source_and_detached_writer_do_no_rendering(preview):
    preview.callbacks.render()
    preview.egress.configure(preview.descriptor)
    callback = next(iter(preview.callbacks.callbacks.values()))
    preview.egress.set_scene_source(object())
    preview.callbacks.render()  # Rendering is disabled by default.
    preview.egress.set_scene_source(None)
    preview.egress.set_enabled(True)
    preview.callbacks.render()
    preview.egress.configure(None)
    callback(1920, 1080)  # A callback already dispatched during removal is harmless.

    preview.renderer.assert_not_called()
    assert preview.writers[0].frames == []
    assert not preview.callbacks.callbacks


@pytest.mark.parametrize("padding", [b"", b"padding!"])
def test_active_callback_preserves_bgra_row_order_and_uses_runtime_canvas(preview, padding):
    first_row, second_row = bytes(range(8)), bytes(range(8, 16))
    preview.renderer.return_value = (first_row + padding + second_row + padding, 8 + len(padding))
    source = object()
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(source)
    preview.egress.set_enabled(True)

    # Callback dimensions describe the main mix; readback uses the configured
    # preview dimensions and runtime canvas, not these callback arguments.
    preview.callbacks.render(640, 480)

    preview.resolve.assert_called_once_with(
        before_render=preview.before_render,
        opaque_background=True,
    )
    preview.renderer.assert_called_once_with(
        source,
        2,
        2,
        canvas_width=1920,
        canvas_height=1080,
    )
    assert preview.writers[0].frames == [(first_row + second_row, 8)]


def test_callback_is_rate_limited_and_enable_and_source_swap_resume_immediately(preview):
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    preview.callbacks.render()
    preview.clock.now += 1 / 60
    preview.callbacks.render()
    assert preview.renderer.call_count == 1

    preview.clock.now += 1 / 30
    preview.callbacks.render()
    assert preview.renderer.call_count == 2
    preview.egress.set_enabled(False)
    preview.callbacks.render()
    assert preview.renderer.call_count == 2
    preview.egress.set_enabled(True)
    preview.callbacks.render()
    assert preview.renderer.call_count == 3

    replacement = object()
    preview.egress.set_scene_source(replacement)
    preview.callbacks.render()
    assert preview.renderer.call_count == 4
    assert preview.renderer.call_args.args[0] is replacement
    assert preview.events.count("register") == 1


def test_same_descriptor_keeps_callback_writer_and_render_deadline(preview):
    preview.egress.configure(preview.descriptor)
    callback = next(iter(preview.callbacks.callbacks))
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    preview.callbacks.render()
    preview.egress.configure(dict(preview.descriptor))
    preview.callbacks.render()

    assert list(preview.callbacks.callbacks) == [callback]
    assert len(preview.writers) == 1
    assert preview.writers[0].close_count == 0
    assert preview.renderer.call_count == 1


def test_new_descriptor_removes_old_callback_before_closing_and_reattaches(preview):
    preview.egress.configure(preview.descriptor)
    first_callback = next(iter(preview.callbacks.callbacks))
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    preview.callbacks.render()
    preview.events.clear()
    preview.egress.configure({**preview.descriptor, "handle_token": "replacement", "width": 3})
    preview.renderer.return_value = (bytes(range(24)), 12)
    preview.callbacks.render()

    assert preview.events[:4] == ["remove start", "remove end", "close", "register"]
    assert first_callback not in preview.callbacks.callbacks
    assert len(preview.callbacks.callbacks) == 1
    assert preview.writers[0].close_count == 1
    assert preview.writers[1].descriptor == (3, 2, "replacement", False)
    assert preview.writers[1].frames == [(bytes(range(24)), 12)]


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        {},
        {
            "transport": "other",
            "handle_token": "preview",
            "width": 2,
            "height": 2,
        },
        {"transport": SHARED_MEMORY_BGRA, "handle_token": "", "width": 2, "height": 2},
        {
            "transport": SHARED_MEMORY_BGRA,
            "handle_token": "preview",
            "width": 0,
            "height": 2,
        },
    ],
)
def test_invalid_descriptor_unregisters_and_closes_once(preview, descriptor):
    preview.egress.configure(preview.descriptor)
    preview.events.clear()
    preview.egress.configure(descriptor)
    preview.egress.shutdown()
    preview.egress.shutdown()

    assert preview.events == ["remove start", "remove end", "close"]
    assert not preview.callbacks.callbacks
    assert preview.writers[0].close_count == 1


def test_unrenderable_frame_is_not_published(preview):
    preview.renderer.return_value = None
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    preview.callbacks.render()

    assert preview.renderer.call_count == 1
    assert preview.writers[0].frames == []


@pytest.mark.parametrize("boundary", ["render", "write"])
def test_callback_logs_boundary_failure_and_recovers_on_next_frame(preview, caplog, boundary):
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    error = RuntimeError(f"{boundary} failed")
    if boundary == "render":
        preview.renderer.side_effect = error
    else:
        preview.writers[0].on_write = Mock(side_effect=error)
    with caplog.at_level("DEBUG", logger=libobs_preview_egress.__name__):
        preview.callbacks.render()  # Binding-side exception suppression is not needed.

    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert preview.writers[0].frames == []
    preview.renderer.side_effect = None
    preview.writers[0].on_write = None
    preview.clock.now += 1
    preview.callbacks.render()
    assert preview.writers[0].frames == [(bytes(range(16)), 8)]


def test_attach_failure_leaves_no_registered_callback_and_can_recover(preview, monkeypatch, caplog):
    writer_factory = content_frame_channel.SharedFrameChannelWriter
    monkeypatch.setattr(
        content_frame_channel,
        "SharedFrameChannelWriter",
        Mock(
            side_effect=FileNotFoundError("preview block missing"),
        ),
    )
    preview.egress.configure(preview.descriptor)

    assert "preview block missing" in caplog.text
    assert not preview.callbacks.callbacks
    assert preview.writers == []
    monkeypatch.setattr(content_frame_channel, "SharedFrameChannelWriter", writer_factory)
    preview.egress.configure(preview.descriptor)
    assert len(preview.callbacks.callbacks) == 1


@pytest.mark.parametrize("boundary", ["resolve", "register"])
def test_registration_failure_closes_attached_writer_logs_and_can_recover(
    preview,
    monkeypatch,
    caplog,
    boundary,
):
    register = preview.callbacks.add_main_render_callback
    error = RuntimeError(f"{boundary} failed")
    if boundary == "resolve":
        preview.resolve.side_effect = error
    else:
        monkeypatch.setattr(preview.callbacks, "add_main_render_callback", Mock(side_effect=error))
    preview.egress.configure(preview.descriptor)

    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert not preview.callbacks.callbacks
    assert preview.writers[0].close_count == 1
    preview.resolve.side_effect = None
    monkeypatch.setattr(preview.callbacks, "add_main_render_callback", register)
    preview.egress.configure(preview.descriptor)
    assert len(preview.callbacks.callbacks) == 1
    assert preview.writers[1].close_count == 0


@pytest.mark.parametrize("operation", ["detach", "reconfigure", "shutdown"])
def test_removal_failure_keeps_live_callback_writer_and_borrowed_source(preview, operation):
    preview.egress.configure(preview.descriptor)
    token = next(iter(preview.callbacks.callbacks))
    source = object()
    preview.egress.set_scene_source(source)
    preview.egress.set_enabled(True)
    error = RuntimeError("native removal failed")
    preview.callbacks.on_remove = Mock(side_effect=error)
    try:
        with pytest.raises(RuntimeError, match="native removal failed"):
            if operation == "shutdown":
                preview.egress.shutdown()
            else:
                descriptor = (
                    None
                    if operation == "detach"
                    else {
                        **preview.descriptor,
                        "handle_token": "replacement",
                    }
                )
                preview.egress.configure(descriptor)
        assert list(preview.callbacks.callbacks) == [token]
        assert len(preview.writers) == 1
        assert preview.writers[0].close_count == 0
        preview.callbacks.render()
        assert preview.renderer.call_args.args[0] is source
        assert preview.writers[0].frames == [(bytes(range(16)), 8)]
    finally:
        preview.callbacks.on_remove = None
    preview.egress.shutdown()
    assert not preview.callbacks.callbacks
    assert preview.writers[0].close_count == 1


def test_successful_shutdown_cannot_be_reconfigured_or_resume_rendering(preview):
    preview.egress.configure(preview.descriptor)
    preview.egress.shutdown()
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)
    preview.egress.configure({**preview.descriptor, "handle_token": "replacement"})
    preview.callbacks.render()

    assert not preview.callbacks.callbacks
    assert len(preview.writers) == 1
    assert preview.writers[0].close_count == 1
    preview.renderer.assert_not_called()


@pytest.mark.parametrize("operation", ["detach", "shutdown"])
def test_callback_removal_can_wait_for_a_callback_before_writer_closes(preview, operation):
    preview.egress.configure(preview.descriptor)
    entered = threading.Event()
    completed = threading.Event()
    callback_threads = []
    observations = []

    def remove_while_callback_is_dispatched(callback):
        def call():
            entered.set()
            try:
                callback(1920, 1080)
            finally:
                completed.set()

        thread = threading.Thread(target=call, daemon=True)
        callback_threads.append(thread)
        thread.start()
        assert entered.wait(1)
        observations.append((completed.wait(1), preview.writers[0].close_count))

    preview.callbacks.on_remove = remove_while_callback_is_dispatched
    try:
        if operation == "detach":
            preview.egress.configure(None)
        else:
            preview.egress.shutdown()
        assert observations == [(True, 0)], "Removal held a lock needed by the callback"
        assert preview.writers[0].close_count == 1
        assert preview.events[-3:] == ["remove start", "remove end", "close"]
    finally:
        for thread in callback_threads:
            thread.join(timeout=1)
            assert not thread.is_alive()


@pytest.mark.parametrize("blocked_phase", ["render", "write"])
def test_source_swap_waits_for_render_and_publication_before_borrowed_release(
    preview, blocked_phase
):
    entered = threading.Event()
    release = threading.Event()
    swap_started = threading.Event()
    swapped = threading.Event()
    errors = []
    source = SimpleNamespace(released=False)
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(source)
    preview.egress.set_enabled(True)

    def block():
        entered.set()
        assert release.wait(2), "Test must release its in-flight frame"
        assert not source.released

    def render(*_args, **_kwargs):
        if blocked_phase == "render":
            block()
        return bytes(range(16)), 8

    preview.renderer.side_effect = render
    if blocked_phase == "write":
        preview.writers[0].on_write = block

    def capture(call):
        try:
            call()
        except Exception as error:  # noqa: BLE001 - forward coordination-thread failures
            errors.append(error)

    def swap():
        swap_started.set()
        preview.egress.set_scene_source(None)
        source.released = True
        swapped.set()

    render_thread = threading.Thread(target=lambda: capture(preview.callbacks.render), daemon=True)
    swap_thread = threading.Thread(target=lambda: capture(swap), daemon=True)
    try:
        render_thread.start()
        assert entered.wait(1)
        swap_thread.start()
        assert swap_started.wait(1)
        assert not swapped.wait(0.05), "Borrowed source was released during its frame"
    finally:
        release.set()
        render_thread.join(timeout=1)
        if swap_thread.ident is not None:
            swap_thread.join(timeout=1)
        assert not render_thread.is_alive()
        assert not swap_thread.is_alive()
    assert errors == []
    assert swapped.is_set() and source.released
    assert preview.writers[0].frames == [(bytes(range(16)), 8)]
    preview.clock.now += 1
    preview.callbacks.render()
    assert preview.renderer.call_count == 1


@pytest.mark.parametrize("operation", ["detach", "shutdown"])
def test_detach_and_shutdown_finish_in_flight_frame_before_closing_writer(preview, operation):
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    errors = []
    preview.egress.configure(preview.descriptor)
    preview.egress.set_scene_source(object())
    preview.egress.set_enabled(True)

    def render(*_args, **_kwargs):
        entered.set()
        assert release.wait(2), "Test must release its in-flight render"
        return bytes(range(16)), 8

    preview.renderer.side_effect = render

    def capture(call):
        try:
            call()
        except Exception as error:  # noqa: BLE001 - forward coordination-thread failures
            errors.append(error)

    def stop():
        if operation == "detach":
            preview.egress.configure(None)
        else:
            preview.egress.shutdown()
        stopped.set()

    render_thread = threading.Thread(target=lambda: capture(preview.callbacks.render), daemon=True)
    stop_thread = threading.Thread(target=lambda: capture(stop), daemon=True)
    try:
        render_thread.start()
        assert entered.wait(1)
        stop_thread.start()
        assert preview.callbacks.remove_requested.wait(1)
        assert not stopped.is_set()
        assert preview.writers[0].close_count == 0
    finally:
        release.set()
        render_thread.join(timeout=1)
        if stop_thread.ident is not None:
            stop_thread.join(timeout=1)
        assert not render_thread.is_alive()
        assert not stop_thread.is_alive()
    assert errors == []
    assert stopped.is_set()
    assert not preview.callbacks.callbacks
    assert preview.writers[0].frames == [(bytes(range(16)), 8)]
    assert preview.writers[0].close_count == 1
    assert preview.events[-4:] == ["write", "remove start", "remove end", "close"]
