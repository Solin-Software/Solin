"""Native thumbnail callbacks with deterministic graphics, refs, clock and writers."""

from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from solin.core.media import obs_source_render
from solin.core.scenes import content_frame_channel, libobs_thumbnail_egress
from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA


class _Callbacks:
    def __init__(self, events):
        self.events = events
        self.callbacks = {}
        self.graphics = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self.remove_requested = threading.Event()
        self.on_remove = None
        self.registration_error = None

    def add_main_render_callback(self, callback):
        if self.registration_error is not None:
            raise self.registration_error
        token = object()
        self.callbacks[token] = callback
        self.events.append("register")
        return token

    def remove_main_render_callback(self, token):
        self.remove_requested.set()
        with self._dispatch_lock:
            callback = self.callbacks[token]
            self.events.append("remove start")
            if self.on_remove is not None:
                self.on_remove(callback)
            del self.callbacks[token]
            self.events.append("remove end")

    def render(self, width=640, height=480):
        with self.graphics, self._dispatch_lock:
            for callback in tuple(self.callbacks.values()):
                callback(width, height)


class _Showing:
    def __init__(self, events):
        self.events = events
        self.counts = Counter()
        self.on_show = None
        self.failed_pointer = None

    def obs_source_inc_showing(self, pointer):
        if pointer == self.failed_pointer:
            raise RuntimeError("show ref failed")
        if self.on_show is not None:
            self.on_show()
        self.counts[pointer] += 1
        self.events.append(("show", pointer))

    def obs_source_dec_showing(self, pointer):
        assert self.counts[pointer] > 0, "Released an unowned showing reference"
        self.counts[pointer] -= 1
        self.events.append(("hide", pointer))


class _Writer:
    def __init__(self, width, height, *, name, create, events):
        self.descriptor = (width, height, name, create)
        self.events = events
        self.frames = []
        self.close_count = 0
        self.on_write = None

    def write(self, data, *, stride):
        assert self.close_count == 0, "Published to a closed atlas"
        if self.on_write is not None:
            self.on_write()
        self.frames.append((bytes(data), stride))
        self.events.append("write")

    def close(self):
        self.close_count += 1
        self.events.append("close")


class _Task:
    """Forward coordination-thread failures and always bound their lifetime."""

    def __init__(self, operation):
        self.started = threading.Event()
        self.done = threading.Event()
        self.errors = []

        def run():
            self.started.set()
            try:
                operation()
            except Exception as error:  # noqa: BLE001 - surface thread assertions to pytest
                self.errors.append(error)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)

    def start(self):
        self.thread.start()
        assert self.started.wait(1)

    def join(self):
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        assert not self.thread.is_alive(), "Coordination thread did not finish"
        assert self.errors == []


@pytest.fixture
def thumbnails(monkeypatch):
    events = []
    callbacks = _Callbacks(events)
    showing = _Showing(events)
    writers = []

    def create_writer(width, height, *, name, create):
        writer = _Writer(width, height, name=name, create=create, events=events)
        writers.append(writer)
        return writer

    monkeypatch.setattr(content_frame_channel, "SharedFrameChannelWriter", create_writer)
    monkeypatch.setattr(
        libobs_thumbnail_egress, "SharedFrameChannelWriter", create_writer, raising=False,
    )
    monkeypatch.setattr("pylibobs._ffi.get_lib", lambda: showing)
    renderer = Mock(return_value=(bytes(range(16)), 8))
    resolve = Mock(return_value=renderer)
    monkeypatch.setattr(obs_source_render, "resolve_render_source_to_bgra", resolve)
    worker = Mock(side_effect=AssertionError("Thumbnails must use native render callbacks"))
    monkeypatch.setattr(
        libobs_thumbnail_egress, "threading",
        SimpleNamespace(
            Lock=threading.Lock, RLock=threading.RLock, Event=threading.Event,
            Thread=worker, current_thread=threading.current_thread,
        ),
    )
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        libobs_thumbnail_egress, "time", SimpleNamespace(monotonic=lambda: clock.now),
        raising=False,
    )
    sources = {sid: SimpleNamespace(_ptr=f"ptr-{sid}") for sid in ("a", "b")}
    resolver = Mock(side_effect=lambda sid: sources.get(sid))
    before_render = Mock(return_value=True)
    runtime = SimpleNamespace(ob=callbacks, video=SimpleNamespace(width=1920, height=1080))
    egress = libobs_thumbnail_egress.LibobsThumbnailEgress(
        runtime, resolver, before_render=before_render,
    )
    descriptor = {
        "transport": SHARED_MEMORY_BGRA, "handle_token": "thumbnails", "width": 2, "height": 4,
    }
    harness = SimpleNamespace(
        egress=egress, callbacks=callbacks, showing=showing, writers=writers, events=events,
        renderer=renderer, resolve=resolve, worker=worker, clock=clock, sources=sources,
        resolver=resolver, before_render=before_render, descriptor=descriptor,
        configure=lambda descriptor=descriptor, scene_ids=("a", "b"), width=2, height=2:
            egress.configure(descriptor, scene_ids, width, height),
    )
    try:
        yield harness
    finally:
        callbacks.on_remove = None
        showing.on_show = None
        egress.shutdown()
        assert all(count == 0 for count in showing.counts.values())


def test_configuration_registers_one_native_callback_and_show_refs_without_worker(thumbnails):
    thumbnails.configure()

    assert len(thumbnails.callbacks.callbacks) == 1
    assert thumbnails.writers[0].descriptor == (2, 4, "thumbnails", False)
    assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}
    thumbnails.worker.assert_not_called()


@pytest.mark.parametrize("padding", [b"", b"padding!"])
def test_atlas_keeps_scene_and_row_order_and_uses_runtime_canvas(thumbnails, padding):
    thumbnails.configure()
    thumbnails.renderer.side_effect = [
        (bytes(range(8)) + padding + bytes(range(8, 16)) + padding, 8 + len(padding)),
        (bytes(range(16, 24)) + padding + bytes(range(24, 32)) + padding, 8 + len(padding)),
    ]

    thumbnails.callbacks.render(320, 180)

    thumbnails.resolve.assert_called_once_with(
        before_render=thumbnails.before_render, opaque_background=True,
    )
    assert thumbnails.renderer.call_args_list == [
        call(thumbnails.sources[sid], 2, 2, canvas_width=1920, canvas_height=1080)
        for sid in ("a", "b")
    ]
    assert thumbnails.writers[0].frames == [(bytes(range(32)), 8)]


@pytest.mark.parametrize("missing", ["missing-source", "no-frame", "render-error"])
def test_unrenderable_cell_keeps_its_atlas_slot_without_losing_other_scenes(thumbnails, missing):
    if missing == "missing-source":
        del thumbnails.sources["a"]
    else:
        thumbnails.renderer.side_effect = [
            None if missing == "no-frame" else RuntimeError("scene render failed"),
            (bytes(range(16)), 8),
        ]
    thumbnails.configure()
    thumbnails.callbacks.render()

    assert thumbnails.writers[0].frames == [(bytes(16) + bytes(range(16)), 8)]
    assert thumbnails.renderer.call_args.args[0] is thumbnails.sources["b"]


def test_cadence_limits_whole_atlas_and_identical_configuration_preserves_deadline(thumbnails):
    thumbnails.configure()
    token = next(iter(thumbnails.callbacks.callbacks))
    thumbnails.callbacks.render()
    thumbnails.clock.now += 0.049
    thumbnails.callbacks.render()
    thumbnails.configure(dict(thumbnails.descriptor))
    thumbnails.callbacks.render()
    assert len(thumbnails.writers[0].frames) == 1
    assert list(thumbnails.callbacks.callbacks) == [token]
    assert len(thumbnails.writers) == 1

    thumbnails.clock.now += 0.002
    thumbnails.callbacks.render()
    assert len(thumbnails.writers[0].frames) == 2
    assert thumbnails.renderer.call_count == 4


def test_suspend_releases_refs_once_and_resume_resolves_new_generation_immediately(thumbnails):
    thumbnails.configure()
    thumbnails.callbacks.render()
    thumbnails.egress.suspend()
    thumbnails.egress.suspend()
    thumbnails.callbacks.render()
    assert thumbnails.showing.counts == {"ptr-a": 0, "ptr-b": 0}
    assert len(thumbnails.writers[0].frames) == 1
    thumbnails.sources.update({sid: SimpleNamespace(_ptr=f"new-{sid}") for sid in ("a", "b")})
    thumbnails.egress.refresh_scene_sources()  # A suspended graph cannot be borrowed.
    assert thumbnails.showing.counts == {"ptr-a": 0, "ptr-b": 0}

    thumbnails.egress.resume()
    thumbnails.egress.resume()
    thumbnails.callbacks.render()
    assert thumbnails.showing.counts == {"ptr-a": 0, "ptr-b": 0, "new-a": 1, "new-b": 1}
    assert len(thumbnails.writers[0].frames) == 2
    assert thumbnails.events.count("register") == 1


def test_refresh_acquires_new_refs_before_hiding_old_and_resets_cadence(thumbnails):
    thumbnails.configure()
    thumbnails.callbacks.render()
    thumbnails.events.clear()
    thumbnails.sources["a"] = SimpleNamespace(_ptr="new-a")
    thumbnails.egress.refresh_scene_sources()

    assert thumbnails.events == [
        ("show", "new-a"), ("show", "ptr-b"), ("hide", "ptr-a"), ("hide", "ptr-b"),
    ]
    thumbnails.callbacks.render()
    assert len(thumbnails.writers[0].frames) == 2
    assert thumbnails.renderer.call_args_list[-2].args[0] is thumbnails.sources["a"]


@pytest.mark.parametrize("failure", ["resolver", "show-ref", "missing-source"])
def test_refresh_failure_rolls_back_partial_refs_and_retains_old_generation(thumbnails, failure):
    thumbnails.configure()
    thumbnails.sources["a"] = SimpleNamespace(_ptr="new-a")
    if failure == "resolver":
        def resolve(sid):
            if sid == "b":
                raise RuntimeError("resolver failed")
            return thumbnails.sources[sid]

        thumbnails.resolver.side_effect = resolve
    elif failure == "show-ref":
        thumbnails.showing.failed_pointer = "ptr-b"
    else:
        del thumbnails.sources["b"]

    thumbnails.egress.refresh_scene_sources()

    assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1, "new-a": 0}
    thumbnails.resolver.side_effect = lambda sid: thumbnails.sources.get(sid)
    thumbnails.showing.failed_pointer = None


@pytest.mark.parametrize("operation", ["detach", "reconfigure", "shutdown"])
def test_removal_failure_preserves_callback_writer_and_show_refs_for_retry(thumbnails, operation):
    thumbnails.configure()
    token = next(iter(thumbnails.callbacks.callbacks))
    thumbnails.callbacks.on_remove = Mock(side_effect=RuntimeError("native removal failed"))
    try:
        with pytest.raises(RuntimeError, match="native removal failed"):
            _stop(thumbnails, operation)
        assert list(thumbnails.callbacks.callbacks) == [token]
        assert len(thumbnails.writers) == 1
        assert thumbnails.writers[0].close_count == 0
        assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}
        thumbnails.callbacks.render()
        assert len(thumbnails.writers[0].frames) == 1
    finally:
        thumbnails.callbacks.on_remove = None
    thumbnails.egress.shutdown()
    assert not thumbnails.callbacks.callbacks
    assert thumbnails.writers[0].close_count == 1


def _stop(thumbnails, operation):
    if operation == "shutdown":
        thumbnails.egress.shutdown()
    else:
        thumbnails.configure(
            None if operation == "detach" else
            {**thumbnails.descriptor, "handle_token": "replacement"},
        )


@pytest.mark.parametrize("boundary", ["resolve", "register"])
def test_registration_failure_closes_writer_balances_refs_and_can_retry(thumbnails, caplog, boundary):
    error = RuntimeError(f"{boundary} failed")
    if boundary == "resolve":
        thumbnails.resolve.side_effect = error
    else:
        thumbnails.callbacks.registration_error = error
    thumbnails.configure()

    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert not thumbnails.callbacks.callbacks
    assert thumbnails.writers[0].close_count == 1
    assert all(count == 0 for count in thumbnails.showing.counts.values())
    thumbnails.resolve.side_effect = None
    thumbnails.callbacks.registration_error = None
    thumbnails.configure()
    assert len(thumbnails.callbacks.callbacks) == 1
    assert thumbnails.writers[1].close_count == 0
    assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}


@pytest.mark.parametrize("operation", ["detach", "shutdown"])
@pytest.mark.parametrize("phase", ["render", "publication"])
def test_teardown_waits_for_in_flight_render_and_publication_before_refs_and_writer_release(
    thumbnails, operation, phase,
):
    thumbnails.configure()
    entered = threading.Event()
    release = threading.Event()

    def block():
        entered.set()
        assert release.wait(2), "Test must release the in-flight atlas"
        assert thumbnails.writers[0].close_count == 0
        assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}

    def render(*_args, **_kwargs):
        if phase == "render":
            block()
        return bytes(range(16)), 8

    thumbnails.renderer.side_effect = render
    if phase == "publication":
        thumbnails.writers[0].on_write = block
    render_task = _Task(thumbnails.callbacks.render)
    stop_task = _Task(lambda: _stop(thumbnails, operation))
    try:
        render_task.start()
        assert entered.wait(1)
        stop_task.start()
        assert thumbnails.callbacks.remove_requested.wait(1)
        assert not stop_task.done.is_set()
        assert thumbnails.writers[0].close_count == 0
        assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}
    finally:
        release.set()
        render_task.join()
        stop_task.join()
    assert thumbnails.writers[0].frames == [(bytes(range(16)) * 2, 8)]
    assert thumbnails.writers[0].close_count == 1
    assert all(count == 0 for count in thumbnails.showing.counts.values())
    assert thumbnails.events.index("write") < thumbnails.events.index("remove end")
    assert thumbnails.events.index("remove end") < thumbnails.events.index("close")


@pytest.mark.parametrize("operation", ["detach", "shutdown"])
def test_removal_never_holds_guard_needed_by_an_already_dispatched_callback(thumbnails, operation):
    thumbnails.configure()
    tasks = []

    def remove(callback):
        task = _Task(lambda: callback(1920, 1080))
        tasks.append(task)
        task.start()
        assert task.done.wait(1), "Removal retained the borrower guard"
        assert thumbnails.writers[0].close_count == 0

    thumbnails.callbacks.on_remove = remove
    try:
        _stop(thumbnails, operation)
    finally:
        for task in tasks:
            task.join()
    assert thumbnails.writers[0].close_count == 1


def test_callback_skips_busy_borrower_guard_and_retries_without_consuming_cadence(thumbnails):
    thumbnails.configure()
    render_task = _Task(thumbnails.callbacks.render)
    try:
        with thumbnails.egress._lock:
            render_task.start()
            assert render_task.done.wait(1), "Graphics callback waited for the control borrower guard"
            thumbnails.renderer.assert_not_called()
            assert thumbnails.writers[0].frames == []
    finally:
        render_task.join()
    thumbnails.callbacks.render()
    assert len(thumbnails.writers[0].frames) == 1


def test_control_handoff_finishes_while_native_graphics_is_owned_before_callback(thumbnails):
    thumbnails.configure()
    thumbnails.sources["a"] = SimpleNamespace(_ptr="new-a")
    handoff = _Task(thumbnails.egress.refresh_scene_sources)
    try:
        with thumbnails.callbacks.graphics:
            handoff.start()
            assert handoff.done.wait(1), "Thumbnail handoff waited for a not-yet-started render"
            thumbnails.renderer.assert_not_called()
    finally:
        handoff.join()
    assert thumbnails.showing.counts == {"ptr-a": 0, "ptr-b": 1, "new-a": 1}
    thumbnails.callbacks.render()
    assert thumbnails.renderer.call_args_list[0].args[0] is thumbnails.sources["a"]


def test_native_callback_during_show_ref_handoff_does_not_invert_graphics_and_guard(thumbnails):
    thumbnails.configure()
    tasks = []

    def callback_during_show():
        task = _Task(thumbnails.callbacks.render)
        tasks.append(task)
        task.start()
        assert task.done.wait(1), "Native showing callback waited for its own control handoff"

    thumbnails.showing.on_show = callback_during_show
    try:
        thumbnails.egress.refresh_scene_sources()
    finally:
        thumbnails.showing.on_show = None
        for task in tasks:
            task.join()
    thumbnails.renderer.assert_not_called()
    thumbnails.callbacks.render()
    assert len(thumbnails.writers[0].frames) == 1


def test_shutdown_is_idempotent_and_cannot_register_or_retain_sources_again(thumbnails):
    thumbnails.configure()
    callback = next(iter(thumbnails.callbacks.callbacks.values()))
    thumbnails.egress.shutdown()
    thumbnails.egress.shutdown()
    thumbnails.egress.resume()
    thumbnails.egress.refresh_scene_sources()
    thumbnails.configure()
    callback(1920, 1080)

    assert not thumbnails.callbacks.callbacks
    assert len(thumbnails.writers) == 1
    assert thumbnails.writers[0].close_count == 1
    thumbnails.renderer.assert_not_called()


def test_reconfigure_removes_old_callback_before_releasing_refs_and_attaching_replacement(thumbnails):
    thumbnails.configure()
    old_token = next(iter(thumbnails.callbacks.callbacks))
    thumbnails.events.clear()
    thumbnails.configure({**thumbnails.descriptor, "handle_token": "replacement"})

    assert thumbnails.events == [
        "remove start", "remove end", ("hide", "ptr-a"), ("hide", "ptr-b"), "close",
        ("show", "ptr-a"), ("show", "ptr-b"), "register",
    ]
    assert old_token not in thumbnails.callbacks.callbacks
    assert len(thumbnails.callbacks.callbacks) == 1
    assert thumbnails.writers[0].close_count == 1
    assert thumbnails.writers[1].descriptor == (2, 4, "replacement", False)
    thumbnails.callbacks.render()
    assert thumbnails.writers[1].frames == [(bytes(range(16)) * 2, 8)]


@pytest.mark.parametrize("invalid", ["descriptor", "scene-ids", "cell-width", "cell-height"])
def test_invalid_configuration_detaches_once_and_dispatched_callback_becomes_harmless(thumbnails, invalid):
    thumbnails.configure()
    callback = next(iter(thumbnails.callbacks.callbacks.values()))
    thumbnails.configure(
        None if invalid == "descriptor" else thumbnails.descriptor,
        () if invalid == "scene-ids" else ("a", "b"),
        0 if invalid == "cell-width" else 2,
        0 if invalid == "cell-height" else 2,
    )
    callback(1920, 1080)
    thumbnails.egress.shutdown()

    assert not thumbnails.callbacks.callbacks
    assert thumbnails.writers[0].close_count == 1
    thumbnails.renderer.assert_not_called()


def test_missing_shared_block_never_registers_or_borrows_sources_and_can_recover(thumbnails, monkeypatch):
    writer_factory = libobs_thumbnail_egress.SharedFrameChannelWriter
    monkeypatch.setattr(
        libobs_thumbnail_egress, "SharedFrameChannelWriter",
        Mock(side_effect=FileNotFoundError("atlas block missing")),
    )
    thumbnails.configure()

    assert not thumbnails.callbacks.callbacks
    assert thumbnails.writers == []
    assert thumbnails.showing.counts == {}
    monkeypatch.setattr(libobs_thumbnail_egress, "SharedFrameChannelWriter", writer_factory)
    thumbnails.configure()
    assert len(thumbnails.callbacks.callbacks) == 1
    assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}


@pytest.mark.parametrize("boundary", ["resolver", "publication"])
def test_callback_boundary_failure_logs_releases_guard_and_recovers_next_frame(thumbnails, caplog, boundary):
    thumbnails.configure()
    error = RuntimeError(f"{boundary} failed")
    if boundary == "resolver":
        thumbnails.resolver.side_effect = error
    else:
        thumbnails.writers[0].on_write = Mock(side_effect=error)
    with caplog.at_level("DEBUG", logger=libobs_thumbnail_egress.__name__):
        thumbnails.callbacks.render()

    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert thumbnails.writers[0].frames == []
    assert thumbnails.egress._lock.acquire(blocking=False), "Callback leaked the borrower guard"
    thumbnails.egress._lock.release()
    thumbnails.resolver.side_effect = lambda sid: thumbnails.sources.get(sid)
    thumbnails.writers[0].on_write = None
    thumbnails.clock.now += 1
    thumbnails.callbacks.render()
    assert thumbnails.writers[0].frames == [(bytes(range(16)) * 2, 8)]


def test_configuration_while_suspended_defers_refs_and_rendering_until_resume(thumbnails):
    thumbnails.egress.suspend()
    thumbnails.configure()
    thumbnails.callbacks.render()
    assert thumbnails.showing.counts == {}
    thumbnails.renderer.assert_not_called()

    thumbnails.egress.resume()
    thumbnails.callbacks.render()
    assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}
    assert thumbnails.writers[0].frames == [(bytes(range(16)) * 2, 8)]


@pytest.mark.parametrize("operation", ["suspend", "refresh"])
def test_source_handoff_waits_for_in_flight_atlas_before_dropping_borrowed_refs(thumbnails, operation):
    thumbnails.configure()
    entered = threading.Event()
    release = threading.Event()

    def render(*_args, **_kwargs):
        entered.set()
        assert release.wait(2), "Test must release the in-flight atlas"
        assert thumbnails.showing.counts["ptr-a"] == 1
        assert thumbnails.showing.counts["ptr-b"] == 1
        return bytes(range(16)), 8

    thumbnails.renderer.side_effect = render
    render_task = _Task(thumbnails.callbacks.render)
    action = (
        thumbnails.egress.suspend if operation == "suspend" else
        thumbnails.egress.refresh_scene_sources
    )
    handoff = _Task(action)
    try:
        render_task.start()
        assert entered.wait(1)
        thumbnails.sources["a"] = SimpleNamespace(_ptr="new-a")
        handoff.start()
        assert not handoff.done.wait(0.05), "Handoff released a borrowed scene during rendering"
        assert thumbnails.showing.counts == {"ptr-a": 1, "ptr-b": 1}
    finally:
        release.set()
        render_task.join()
        handoff.join()
    assert thumbnails.writers[0].frames == [(bytes(range(16)) * 2, 8)]
    assert thumbnails.showing.counts["ptr-a"] == 0
    assert thumbnails.showing.counts["ptr-b"] == (0 if operation == "suspend" else 1)
    assert thumbnails.showing.counts["new-a"] == (0 if operation == "suspend" else 1)
