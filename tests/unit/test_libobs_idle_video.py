"""Native idle-video preparation and held-frame lifetimes without libobs decode."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.media.obs_runtime import ObsRuntime
from solin.core.scenes import libobs_idle_video as module
from solin.core.scenes.libobs_idle_video import IdleVideoError, LibobsIdleVideo


class _Source:
    def __init__(self, runtime, kind, settings):
        self.runtime, self.kind = runtime, kind
        self.initial, self.settings = dict(settings), dict(settings)
        self.updates = []
        self.events = ["create"]
        self.observers = []
        self.pending = False
        self.width = self.height = 0
        self.frame = None
        self.muted, self.volume, self.audio_mixers = False, 1.0, 63
        self.showing = self.released = self.restarts = 0
        self.play_pause = []
        self.fail_release = self.fail_disconnect = self.fail_unshowing = False
        self.fail_restart = self.fail_pause = False
        runtime.sources.append(self)
        if kind == "ffmpeg_source":
            runtime.decoders.append(self)
            runtime.peak_decoders = max(
                runtime.peak_decoders, sum(not source.released for source in runtime.decoders),
            )

    def update(self, settings):
        assert self.muted and self.volume == 0 and self.audio_mixers == 0
        assert "monitor:0" in self.events
        assert self.observers, "Observer must precede the first deferred file open"
        self.settings.update(settings)
        self.updates.append(dict(settings))
        self.events.append("update")
        self.pending = True

    def media_play_pause(self, pause):
        self.play_pause.append(pause)
        self.events.append(f"pause:{pause}")
        if self.fail_pause:
            raise RuntimeError("Pause failed")

    def media_restart(self):
        assert self.showing > 0, "An invisible decoder cannot process media_restart"
        self.restarts += 1
        self.frame = "initial"
        self.events.append("restart")
        if self.fail_restart:
            raise RuntimeError("Restart failed after queueing")

    def release(self):
        assert not self.observers, "Callbacks must be disconnected before decoder release"
        assert self.showing == 0, "Manual showing references must be balanced"
        assert not any(item.source is self for scene in self.runtime.scenes for item in scene.items)
        assert not self.released, "A source must be released exactly once"
        if self.fail_release:
            raise RuntimeError("Release failed")
        self.released += 1
        self.events.append("release")


class _Item:
    def __init__(self, scene, source):
        self.scene, self.source = scene, source
        self.removed = False
        self.released = 0

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        if self.scene.runtime.fail_transform:
            raise RuntimeError("Transform failed")
        self._bounds = value

    def remove(self):
        assert self.scene.atomic
        self.scene.items.remove(self)
        self.removed = True

    def release(self):
        assert self.removed and not self.released
        self.released += 1


class _Scene:
    def __init__(self, runtime):
        self.runtime = runtime
        self.items, self.snapshots = [], []
        self.atomic = False
        self.released = 0
        self.view = SimpleNamespace(width=runtime.video.width, height=runtime.video.height)
        runtime.scenes.append(self)

    def as_source(self):
        return self.view

    def add(self, source):
        assert self.atomic and source.width > 0 and source.height > 0
        item = _Item(self, source)
        self.items.append(item)
        return item

    def release(self):
        assert not self.items and not self.released
        self.released += 1


class _Still:
    def __init__(self, runtime):
        self.source = _Source(runtime, "held_frame", {})
        self.uploads = []

    def push_bgra(self, data, width, height, stride, *, reset):
        assert reset, "The immutable first frame must be synchronously uploaded"
        self.uploads.append((data, width, height, stride, reset))
        self.source.width, self.source.height = width, height
        self.source.frame = "initial"
        return True

    def release(self):
        self.source.release()


class _Runtime:
    def __init__(self):
        self.video = SimpleNamespace(width=1920, height=1080)
        self.sources, self.decoders, self.scenes = [], [], []
        self.peak_decoders = 0
        self.now_ms = 1000
        self.no_ack = self.no_frame = self.no_second_ack = False
        self.fail_monitor = self.fail_transform = False
        self.prime_calls = []
        self.block_reprepare = False
        self.reprepare_entered, self.resume_reprepare = threading.Event(), threading.Event()
        self.reprepare_ready = threading.Event()
        self.capture_calls = []
        self.ob = SimpleNamespace(
            Source=SimpleNamespace(create_private=self.create_source),
            Scene=SimpleNamespace(create_private=lambda _name: _Scene(self)),
            BoundsType=SimpleNamespace(SCALE_INNER=2),
        )

    def create_source(self, kind, _name, settings):
        return _Source(self, kind, settings)

    def set_source_monitoring(self, source, value):
        if self.fail_monitor:
            raise RuntimeError("Monitoring failed")
        source.events.append(f"monitor:{value}")

    def observe_source_updates(self, source, callback):
        source.observers.append(callback)
        source.events.append("observe")

        def disconnect():
            if source.fail_disconnect:
                raise RuntimeError("Disconnect failed")
            source.observers.remove(callback)
            source.events.append("disconnect")

        return disconnect

    def prime_initial_source_video(self, source):
        self.prime_calls.append(source)
        if not self.no_frame and source.events.count("tick") >= 2:
            source.width, source.height = 4, 2
            source.frame = "initial"
            return True
        return False

    def retain_source_showing(self, source):
        source.showing += 1
        source.events.append("showing")
        if len(self.decoders) > 1:
            self.reprepare_ready.set()

        released = False

        def release():
            nonlocal released
            if released:
                return
            if source.fail_unshowing:
                raise RuntimeError("Showing release failed")
            source.showing -= 1
            source.events.append("unshowing")
            released = True

        return release

    @staticmethod
    def atomic_scene_update(scene, callback):
        scene.atomic = True
        try:
            callback()
        finally:
            scene.atomic = False
            scene.snapshots.append(tuple((item.source, item.source.frame) for item in scene.items))

    def wait(self, video, _deadline):
        if self.block_reprepare and len(self.decoders) > 1:
            self.reprepare_entered.set()
            while not self.resume_reprepare.is_set() and not video._closed.wait(0.01):
                pass
        self.now_ms += 10
        for source in tuple(self.decoders):
            if source.released:
                continue
            source.events.append("tick")
            if source.pending and not self.no_ack and not (
                self.no_second_ack and len(source.updates) == 2
            ):
                source.pending = False
                for callback in tuple(source.observers):
                    callback()


@pytest.fixture
def video_helper(monkeypatch, tmp_path):
    path = tmp_path / "idle.mp4"
    path.write_bytes(b"Native decoder is replaced by deterministic source doubles")
    runtime = _Runtime()
    video = LibobsIdleVideo(runtime, "test-idle", str(path))
    monkeypatch.setattr(module.time, "monotonic", lambda: runtime.now_ms / 1000)
    monkeypatch.setattr(video, "_wait", lambda deadline: runtime.wait(video, deadline))

    def capture(source, width, height, **kwargs):
        assert source.frame == "initial" and source.play_pause[-1] is True
        runtime.capture_calls.append((source, width, height, kwargs))
        return bytes([23, 41, 67, 255]) * width * height, width * 4

    monkeypatch.setattr(module, "render_source_to_bgra", capture)
    monkeypatch.setattr(module, "create_frame_source", lambda _runtime, _name: _Still(runtime))
    yield video, runtime, path
    video.close()


def _prepare(video):
    video.prepare(2000, lambda: None)


def _await_decoder(video, *, previous=None):
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline:
        decoder = video.decoder
        if decoder is not None and decoder is not previous:
            return decoder
        threading.Event().wait(0.005)
    pytest.fail("Replacement decoder was not published")


@pytest.mark.parametrize("hardware", ["1", "0"])
def test_decoder_audio_is_disabled_before_open_and_same_file_ack_does_not_reopen(
    video_helper, monkeypatch, hardware,
):
    video, runtime, path = video_helper
    monkeypatch.setenv("SOLIN_MEDIA_HW_DECODE", hardware)
    _prepare(video)
    source = video.decoder
    assert source.initial == {
        "is_local_file": True, "local_file": "", "looping": True,
        "speed_percent": 100, "restart_on_activate": True,
        "close_when_inactive": False, "clear_on_media_end": False,
        "hw_decode": hardware == "1",
    }
    assert source.muted and source.volume == 0 and source.audio_mixers == 0
    assert source.events.index("monitor:0") < source.events.index("update")
    assert source.events.index("observe") < source.events.index("update")
    assert source.updates[0]["local_file"] == str(path)
    assert source.updates[1:] == [{"restart_on_activate": False}]
    assert not source.observers and source.events.count("disconnect") == 1
    assert source.play_pause == [True] and source.restarts == 0
    assert len(runtime.prime_calls) >= 2


def test_prepared_scene_holds_one_initial_frame_and_preserves_full_canvas_geometry(video_helper):
    video, runtime, _path = video_helper
    assert not runtime.sources and not runtime.scenes
    _prepare(video)
    scene = runtime.scenes[0]
    still = scene.items[0].source
    assert still.kind == "held_frame" and still.frame == "initial"
    assert (video.source.width, video.source.height) == (1920, 1080)
    assert scene.items[0].bounds == (1920, 1080)
    assert scene.items[0].bounds_type == 2 and scene.items[0].bounds_alignment == 0
    assert len(runtime.capture_calls) == 1 and runtime.peak_decoders == 1
    for _ in range(50):
        video.set_live(False)
    assert video.decoder.restarts == 0 and len(scene.snapshots) == 1


def test_first_live_entry_publishes_prepared_decoder_and_additional_consumers_share_clock(video_helper):
    video, runtime, _path = video_helper
    _prepare(video)
    stable = video.source
    decoder = video.decoder
    video.set_live(True)
    decoder.frame = "later-in-loop"
    for _ in range(50):
        video.set_live(True)
    assert video.source is stable
    assert decoder.restarts == 1 and decoder.showing == 1
    assert runtime.scenes[0].items[0].source is decoder
    assert runtime.scenes[0].snapshots[-1] == ((decoder, "initial"),)


def test_rapid_live_return_holds_initial_frame_until_only_replacement_decoder_is_prepared(video_helper):
    video, runtime, _path = video_helper
    _prepare(video)
    stable, old = video.source, video.decoder
    video.set_live(True)
    old.frame = "later-in-loop"
    runtime.block_reprepare = True
    video.set_live(False)
    assert runtime.reprepare_entered.wait(1), "Cold preparation did not begin"
    scene = runtime.scenes[0]
    still = scene.items[0].source
    assert still.kind == "held_frame" and still.frame == "initial"
    assert old.released == 1 and old.showing == 0
    returned = threading.Event()
    caller = threading.Thread(target=lambda: (video.set_live(True), returned.set()))
    try:
        caller.start()
        assert returned.wait(1), "Render demand blocked on decoder preparation"
        assert scene.items[0].source is still and video.decoder is None
        runtime.resume_reprepare.set()
        assert runtime.reprepare_ready.wait(1)
        replacement = _await_decoder(video, previous=old)
        assert replacement is not None and replacement is not old
        video.set_live(True)
        assert video.source is stable and scene.items[0].source is replacement
        assert scene.snapshots[-1] == ((replacement, "initial"),)
        assert replacement.restarts == 1
        assert runtime.peak_decoders == 1 and len(runtime.capture_calls) == 1
    finally:
        runtime.resume_reprepare.set()
        caller.join(1)


@pytest.mark.parametrize("failure", ["no_ack", "no_frame", "no_second_ack"])
def test_missing_preparation_ack_or_initial_frame_times_out_without_publishing(video_helper, failure):
    video, runtime, _path = video_helper
    setattr(runtime, failure, True)
    with pytest.raises(IdleVideoError, match="deadline_exceeded"):
        video.prepare(1100, lambda: None)
    assert not runtime.scenes and not runtime.capture_calls
    assert runtime.decoders[0].released == 1 and not runtime.decoders[0].observers


def test_stale_preparation_disconnects_callbacks_and_releases_decoder(video_helper):
    video, runtime, _path = video_helper

    def current():
        if runtime.now_ms >= 1010:
            raise IdleVideoError("stale_revision")

    with pytest.raises(IdleVideoError, match="stale_revision"):
        video.prepare(2000, current)
    assert not runtime.scenes
    assert runtime.decoders[0].released == 1 and not runtime.decoders[0].observers


def test_missing_media_is_rejected_before_allocating_native_resources(video_helper):
    video, runtime, path = video_helper
    path.unlink()
    with pytest.raises(IdleVideoError, match="idle_media_unavailable"):
        _prepare(video)
    assert not runtime.sources and not runtime.scenes


@pytest.mark.parametrize("failure", ["missing_file", "monitor"])
def test_failed_cold_reprepare_preserves_initial_frame_and_reports_error(
    video_helper, monkeypatch, failure,
):
    video, runtime, path = video_helper
    _prepare(video)
    scene = runtime.scenes[0]
    still = scene.items[0].source
    video.set_live(True)
    if failure == "missing_file":
        path.unlink()
    else:
        runtime.fail_monitor = True
    reported = threading.Event()
    monkeypatch.setattr(module.log, "warning", lambda *_args, **_kwargs: reported.set())

    video.set_live(False)

    assert reported.wait(1), "Cold preparation failure was not reported"
    video.set_live(True)
    assert scene.items[0].source is still and still.frame == "initial"
    assert video.decoder is None
    assert video.error_code == (
        "idle_media_unavailable" if failure == "missing_file" else "idle_source_unavailable"
    )
    assert runtime.peak_decoders == 1
    assert all(source.released == 1 and source.showing == 0 for source in runtime.decoders)


@pytest.mark.parametrize("failure", ["monitor", "capture", "frame_source", "upload", "transform"])
def test_failed_initial_preparation_releases_all_partial_native_resources(
    video_helper, monkeypatch, failure,
):
    video, runtime, _path = video_helper
    if failure == "monitor":
        runtime.fail_monitor = True
    elif failure == "capture":
        monkeypatch.setattr(module, "render_source_to_bgra", lambda *_args, **_kwargs: None)
    elif failure == "frame_source":
        monkeypatch.setattr(module, "create_frame_source", lambda *_args: None)
    elif failure == "upload":
        monkeypatch.setattr(_Still, "push_bgra", lambda *_args, **_kwargs: False)
    else:
        runtime.fail_transform = True
    with pytest.raises((IdleVideoError, RuntimeError)):
        _prepare(video)
    assert all(source.released == 1 and not source.observers for source in runtime.sources)
    assert all(scene.released == 1 and not scene.items for scene in runtime.scenes)


def test_shutdown_drains_blocked_reprepare_and_balances_native_showing_references(video_helper):
    video, runtime, _path = video_helper
    _prepare(video)
    video.set_live(True)
    runtime.block_reprepare = True
    video.set_live(False)
    assert runtime.reprepare_entered.wait(1)
    closing = threading.Thread(target=video.close)
    closing.start()
    closing.join(1)
    assert not closing.is_alive(), "Shutdown did not cancel cold decoder preparation"
    video.close()
    video.set_live(True)
    assert all(source.released == 1 and source.showing == 0 for source in runtime.sources)
    assert all(not source.observers for source in runtime.sources)
    assert runtime.scenes[0].released == 1 and not runtime.scenes[0].items


def test_render_demand_returns_without_waiting_for_helper_control_lock(video_helper):
    video, _runtime, _path = video_helper
    _prepare(video)
    returned = threading.Event()
    with video._lock:
        caller = threading.Thread(target=lambda: (video.set_live(True), returned.set()))
        caller.start()
        assert returned.wait(1), "Render demand blocked on the helper control lock"
    caller.join(1)
    assert video.decoder.restarts == 0
    video.set_live(True)
    assert video.decoder.restarts == 1


@pytest.mark.parametrize("failure", ["disconnect", "release"])
def test_failed_initial_native_cleanup_retains_ownership_until_close_retry(
    video_helper, monkeypatch, failure,
):
    video, runtime, _path = video_helper
    create = runtime.create_source

    def failing_source(kind, name, settings):
        source = create(kind, name, settings)
        setattr(source, f"fail_{failure}", True)
        return source

    monkeypatch.setattr(runtime.ob.Source, "create_private", failing_source)
    runtime.fail_monitor = failure == "release"
    with pytest.raises(RuntimeError, match="Disconnect failed|Release failed"):
        _prepare(video)
    decoder = runtime.decoders[0]
    assert decoder.released == 0
    assert bool(decoder.observers) == (failure == "disconnect")
    with pytest.raises(RuntimeError, match="Disconnect failed|Release failed"):
        video.close()
    assert decoder.released == 0
    setattr(decoder, f"fail_{failure}", False)
    video.close()
    video.close()
    assert decoder.released == 1 and not decoder.observers
    assert decoder.showing == 0 and len(runtime.decoders) == 1


@pytest.mark.parametrize("failure", ["unshowing", "release"])
def test_partial_close_retries_only_unreleased_native_resources(video_helper, failure):
    video, runtime, _path = video_helper
    _prepare(video)
    decoder = video.decoder
    setattr(decoder, f"fail_{failure}", True)
    with pytest.raises(RuntimeError, match="Showing release failed|Release failed"):
        video.close()
    assert decoder.released == 0
    assert decoder.showing == (1 if failure == "unshowing" else 0)
    assert runtime.scenes[0].released == 1 and not runtime.scenes[0].items
    setattr(decoder, f"fail_{failure}", False)
    video.close()
    video.close()
    assert all(source.released == 1 and source.showing == 0 for source in runtime.sources)
    assert decoder.events.count("unshowing") == 1
    assert runtime.scenes[0].released == 1


@pytest.mark.parametrize("failure", ["monitor", "showing"])
def test_failed_cold_candidate_cleanup_remains_owned_for_shutdown_retry(
    video_helper, monkeypatch, failure,
):
    video, runtime, _path = video_helper
    _prepare(video)
    video.set_live(True)
    old = video.decoder
    create = runtime.create_source

    def failing_source(kind, name, settings):
        source = create(kind, name, settings)
        source.fail_release = True
        return source

    monkeypatch.setattr(runtime.ob.Source, "create_private", failing_source)
    if failure == "monitor":
        runtime.fail_monitor = True
    else:
        monkeypatch.setattr(
            runtime, "retain_source_showing",
            Mock(side_effect=RuntimeError("Showing acquisition failed")),
        )
    reported = threading.Event()

    def warning(message, *_args, **_kwargs):
        if message == (
            "Could not prepare idle video start" if failure == "monitor"
            else "Could not release prepared idle decoder"
        ):
            reported.set()

    monkeypatch.setattr(module.log, "warning", warning)
    video.set_live(False)
    assert reported.wait(2), "Failed cold candidate was not retained/reported"
    candidate = runtime.decoders[1]
    assert old.released == 1 and candidate.released == 0
    assert video.decoder is None and video.error_code == "idle_source_unavailable"
    assert runtime.scenes[0].items[0].source.kind == "held_frame"
    with pytest.raises(RuntimeError, match="Release failed"):
        video.close()
    candidate.fail_release = False
    video.close()
    assert all(source.released == 1 and source.showing == 0 for source in runtime.sources)
    assert all(not source.observers for source in runtime.sources)
    assert runtime.peak_decoders == 1


@pytest.mark.parametrize("failure", ["unshowing", "release"])
def test_failed_retirement_keeps_old_decoder_owned_and_never_opens_a_second_one(
    video_helper, monkeypatch, failure,
):
    video, runtime, _path = video_helper
    _prepare(video)
    old = video.decoder
    video.set_live(True)
    old.frame = "later-in-loop"
    setattr(old, f"fail_{failure}", True)
    reported = threading.Event()
    monkeypatch.setattr(module.log, "warning", lambda *_args, **_kwargs: reported.set())
    video.set_live(False)
    assert reported.wait(2)
    assert video.decoder is None and video.error_code == "idle_source_unavailable"
    assert old.released == 0 and len(runtime.decoders) == 1
    video.set_live(True)
    assert runtime.scenes[0].items[0].source.kind == "held_frame"
    assert old.restarts == 1
    with pytest.raises(RuntimeError, match="Showing release failed|Release failed"):
        video.close()
    setattr(old, f"fail_{failure}", False)
    video.close()
    video.close()
    assert all(source.released == 1 and source.showing == 0 for source in runtime.sources)
    assert old.events.count("unshowing") == 1
    assert len(runtime.decoders) == 1


def test_failed_live_transform_does_not_restart_or_invalidate_prepared_decoder(video_helper):
    video, runtime, _path = video_helper
    _prepare(video)
    scene = runtime.scenes[0]
    still, decoder = scene.items[0].source, video.decoder
    runtime.fail_transform = True
    try:
        with pytest.raises(RuntimeError, match="Transform failed"):
            video.set_live(True)
        assert scene.items[0].source is still and still.frame == "initial"
        assert decoder.restarts == 0 and decoder.released == 0
        assert len(runtime.decoders) == 1
    finally:
        runtime.fail_transform = False
    video.set_live(True)
    assert scene.items[0].source is decoder and decoder.restarts == 1
    assert scene.snapshots[-1] == ((decoder, "initial"),)


@pytest.mark.parametrize("failure", ["restart", "pause"])
def test_failed_native_playback_hides_and_retires_possibly_started_decoder(video_helper, failure):
    video, runtime, _path = video_helper
    _prepare(video)
    old = video.decoder
    scene = runtime.scenes[0]
    still = scene.items[0].source
    if failure == "pause":
        video.set_live(True)
    setattr(old, f"fail_{failure}", True)
    runtime.block_reprepare = True
    try:
        with pytest.raises(RuntimeError, match="Restart failed|Pause failed"):
            video.set_live(failure == "restart")
        assert runtime.reprepare_entered.wait(2)
        assert scene.items[0].source is still and still.frame == "initial"
        assert old.released == 1 and old.showing == 0 and video.decoder is None
        video.set_live(True)
        assert scene.items[0].source is still and old.restarts == 1
        runtime.resume_reprepare.set()
        replacement = _await_decoder(video, previous=old)
        video.set_live(True)
        assert scene.items[0].source is replacement and replacement.restarts == 1
        assert scene.snapshots[-1] == ((replacement, "initial"),)
        assert runtime.peak_decoders == 1
    finally:
        runtime.resume_reprepare.set()


def test_close_drains_concurrent_initial_capture_before_releasing_decoder(
    video_helper, monkeypatch,
):
    video, runtime, _path = video_helper
    entered, resume, closed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    capture = module.render_source_to_bgra

    def blocked_capture(source, *args, **kwargs):
        entered.set()
        assert resume.wait(2), "Initial native capture was not released"
        assert source.released == 0, "Shutdown released a source still being captured"
        return capture(source, *args, **kwargs)

    monkeypatch.setattr(module, "render_source_to_bgra", blocked_capture)

    def prepare():
        try:
            _prepare(video)
        except IdleVideoError as error:
            errors.append(error.error_code)

    def close():
        video.close()
        closed.set()

    preparing = threading.Thread(target=prepare)
    closing = threading.Thread(target=close)
    try:
        preparing.start()
        assert entered.wait(2)
        closing.start()
        assert video._closed.wait(2)
        assert not closed.is_set(), "close() returned before initial capture drained"
        assert runtime.decoders[0].released == 0
        resume.set()
        preparing.join(2)
        closing.join(2)
        assert not preparing.is_alive() and not closing.is_alive()
        assert closed.is_set() and errors == ["closed"]
        assert not runtime.scenes
        assert all(source.released == 1 and not source.observers for source in runtime.sources)
        assert all(source.showing == 0 for source in runtime.sources)
        video.close()
    finally:
        resume.set()
        if preparing.ident is not None:
            preparing.join(2)
        if closing.ident is not None:
            closing.join(2)


@pytest.mark.parametrize("exception", [None, RuntimeError("transaction failed"), KeyboardInterrupt()])
def test_native_atomic_scene_boundary_uses_public_callback_and_propagates_errors(monkeypatch, exception):
    from pylibobs import _ffi

    pointer = _ffi.ffi.cast("obs_scene_t *", 123)
    calls = []

    def atomic(scene, callback, data):
        calls.append(scene)
        callback(data, scene)

    native = Mock()
    monkeypatch.setattr(_ffi, "get_lib", lambda: SimpleNamespace(obs_scene_atomic_update=native))
    original_cast = _ffi.ffi.cast

    def cast(signature, value):
        if value is native:
            return atomic
        return original_cast(signature, value)

    monkeypatch.setattr(_ffi.ffi, "cast", cast)

    def update():
        if exception is not None:
            raise exception

    if exception is None:
        ObsRuntime.atomic_scene_update(SimpleNamespace(_ptr=pointer), update)
    else:
        with pytest.raises(type(exception), match=str(exception) or None):
            ObsRuntime.atomic_scene_update(SimpleNamespace(_ptr=pointer), update)
    assert calls == [pointer]
