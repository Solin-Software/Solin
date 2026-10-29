"""Unit tests for the Qt-free libobs media source (ffmpeg_source wrapper)."""

from __future__ import annotations

import types
import time

import pytest

from solin.core.scenes import libobs_media_source
from solin.core.scenes.libobs_media_source import (
    _PROGRESSIVE_FFMPEG_OPTIONS,
    STATE_NONE,
    STATE_PAUSED,
    STATE_PLAYING,
    LibobsMediaSource,
)


class _FakeMediaSource:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.initial_settings = dict(settings)
        self.lifecycle = ["create"]
        self._ptr = object()  # stands in for the raw obs_source_t*
        self.play_pause: list[bool] = []
        self.stops = 0
        self.restarts = 0
        self.updates: list[dict] = []
        self.released = 0
        self._media_time = 0
        self.seeks: list[int] = []
        self.width = self.height = 0
        self.media_duration = 0
        self.media_state = STATE_PLAYING

    def media_play_pause(self, pause: bool) -> None:
        self.play_pause.append(pause)

    @property
    def media_time(self) -> int:
        return self._media_time

    @media_time.setter
    def media_time(self, value: int) -> None:
        self.seeks.append(value)
        self._media_time = value

    def media_stop(self) -> None:
        self.lifecycle.append("stop")
        self.stops += 1

    def media_restart(self) -> None:
        self.lifecycle.append("restart")
        self.restarts += 1

    def update(self, settings: dict) -> None:
        self.lifecycle.append("update")
        self.updates.append(dict(settings))
        self.settings.update(settings)

    def release(self) -> None:
        self.lifecycle.append("release")
        self.released += 1


class _FakeVideoReadiness:
    def __init__(self, source) -> None:
        self.source = source
        self.ready = False
        self.invalidations = 0
        self.closed = False
        self.updating = False
        self._updated_once = False
        source.lifecycle.append("watch")

    def update(self, settings):
        if self._updated_once:
            self.invalidate()
        self.source.update(settings)
        self._updated_once = True

    def invalidate(self) -> None:
        self.invalidations += 1
        self.ready = False
        self.source.lifecycle.append("invalidate")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.source.lifecycle.append("unwatch")


class _Runtime:
    def __init__(self) -> None:
        self.created: list[_FakeMediaSource] = []
        self.watches: list[_FakeVideoReadiness] = []
        self.monitored: list = []
        runtime = self

        class _SourceNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict) -> _FakeMediaSource:
                source = _FakeMediaSource(kind, name, settings)
                runtime.created.append(source)
                return source

        self.ob = types.SimpleNamespace(
            Source=_SourceNS, enum_source_types=lambda: [libobs_media_source.MEDIA_SOURCE_ID],
        )

    def watch_source_video(self, source):
        watch = _FakeVideoReadiness(source)
        self.watches.append(watch)
        return watch

    def set_source_monitoring(self, source, monitoring_type) -> None:
        self.monitored.append((source, monitoring_type))


@pytest.mark.parametrize("state", [STATE_PLAYING, STATE_PAUSED])
def test_visual_readiness_requires_a_decoded_frame_even_when_transport_is_ready(state):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/video.mp4")
    source = media.source
    source.media_state = state
    source.width = source.height = 0
    try:
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        source.width, source.height = 160, 90
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        runtime.watches[-1].ready = True
        assert media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


@pytest.mark.parametrize("state", [
    libobs_media_source.STATE_ERROR,
    libobs_media_source.STATE_ENDED,
    libobs_media_source.STATE_STOPPED,
])
def test_visual_readiness_rejects_terminal_decoders_even_with_cached_dimensions(state):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/invalid.mp4")
    source = media.source
    source.width, source.height = 160, 90
    source.media_state = state
    runtime.watches[-1].ready = True
    try:
        assert not media.wait_for_video_frame(deadline=time.monotonic() + 10)
    finally:
        media.close()


def test_visual_readiness_waits_for_gpu_priming_and_reuses_the_primed_source():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/video.mp4")
    source = media.source
    source.width, source.height = 160, 90
    watch = runtime.watches[-1]
    try:
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        watch.ready = True
        assert media.wait_for_video_frame(deadline=time.monotonic())
        assert media.wait_for_video_frame(deadline=time.monotonic())
        assert runtime.watches == [watch] and watch.source is source
        source.media_state = libobs_media_source.STATE_ERROR
        assert not media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


@pytest.mark.parametrize("change", ["stop", "restart"])
def test_decoder_reset_invalidates_priming_even_when_the_source_pointer_is_unchanged(change):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("https://cdn.example/video.mp4")
    source = media.source
    source.width, source.height = 160, 90
    watch = runtime.watches[-1]
    watch.ready = True
    try:
        assert media.wait_for_video_frame(deadline=time.monotonic())
        getattr(media, change)()
        source.media_state = STATE_PLAYING
        assert media.source is source
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert watch.invalidations == 1 and not watch.ready
        assert source.lifecycle[-2:] == ["invalidate", change]
        watch.ready = True
        assert media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


def test_open_local_file_creates_ffmpeg_source_and_autoplays(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)  # default: HW on
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("/tmp/clip.mp4", autoplay=True) is True

    source = runtime.created[-1]
    assert source.kind == "solin_media_source"
    assert source.settings == {
        "is_local_file": True, "local_file": "/tmp/clip.mp4", "hw_decode": True,
        "restart_on_activate": False,
    }
    assert source.play_pause == []  # The input update starts native playback.
    assert media.state == STATE_PLAYING
    assert source.volume == 1.0
    assert runtime.monitored == [(source, runtime.monitored[0][1])]  # monitoring set
    assert media.source is source
    assert media.path == "/tmp/clip.mp4"
    assert source.initial_settings == {**source.settings, "local_file": ""}
    assert source.updates == [source.settings]


def test_missing_native_decoder_rejects_open_without_an_inert_source():
    runtime = _Runtime()
    runtime.ob.enum_source_types = lambda: ["ffmpeg_source"]
    media = LibobsMediaSource(runtime)
    assert not media.open("/song.mp3")
    assert media.source is None and not runtime.created


def test_open_http_file_uses_finite_transport_and_can_start_paused(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)  # default: HW on
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("https://cdn.example/v.mp4", autoplay=False) is True

    source = runtime.created[-1]
    assert source.settings == {
        "is_local_file": True, "local_file": "https://cdn.example/v.mp4", "hw_decode": True,
        "restart_on_activate": False,
        "ffmpeg_options": _PROGRESSIVE_FFMPEG_OPTIONS,
    }
    assert source.play_pause == []  # Pause must wait for the decoder's initial reset.
    assert source.seeks == []
    assert source.volume == 0.0
    assert media.state == STATE_PAUSED and source.media_state == STATE_PLAYING
    assert source.initial_settings == {**source.settings, "local_file": ""}
    assert source.updates == [source.settings]


@pytest.mark.parametrize("path, input_key", [
    ("/clip.mp4", "local_file"),
    ("https://cdn.example/clip.mp4", "local_file"),
])
def test_readiness_observer_is_installed_before_the_input_can_decode(path, input_key, monkeypatch):
    runtime = _Runtime()
    original_update = _FakeMediaSource.update

    def update(source, settings):
        assert source.initial_settings[input_key] == ""
        assert source.lifecycle == ["create", "watch"]
        assert len(runtime.watches) == 1
        assert runtime.watches[0].source is source
        assert not runtime.watches[0].closed
        assert settings[input_key] == path
        original_update(source, settings)

    monkeypatch.setattr(_FakeMediaSource, "update", update)
    media = LibobsMediaSource(runtime)
    try:
        assert media.open(path)
        assert runtime.created[0].lifecycle == ["create", "watch", "update"]
    finally:
        media.close()


@pytest.mark.parametrize("failure", ["watch", "update"])
def test_failed_readiness_setup_releases_the_unopened_candidate(failure, monkeypatch, caplog):
    runtime = _Runtime()
    original_watch = runtime.watch_source_video
    original_update = _FakeMediaSource.update

    def fail(*_args):
        raise RuntimeError("setup failed")

    if failure == "watch":
        monkeypatch.setattr(runtime, "watch_source_video", fail)
    else:
        monkeypatch.setattr(_FakeMediaSource, "update", fail)
    media = LibobsMediaSource(runtime)
    assert not media.open("/clip.mp4", autoplay=False)
    source = runtime.created[0]
    assert source.stops == source.released == 1
    assert media.source is None and media.path == ""
    assert "Could not open ffmpeg_source input" in caplog.text
    if failure == "update":
        assert runtime.watches[0].closed
        assert source.lifecycle[-3:] == ["unwatch", "stop", "release"]
    media.close()
    assert source.released == 1
    monkeypatch.setattr(runtime, "watch_source_video", original_watch)
    monkeypatch.setattr(_FakeMediaSource, "update", original_update)
    assert media.open("/next.mp4", autoplay=True)
    assert media.source.volume == 1.0 and media.state == STATE_PLAYING
    media.close()


@pytest.mark.parametrize("poll", ["resume", "video_wait"])
def test_initial_pause_waits_for_native_readiness_and_pause_acknowledgement(poll):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4", autoplay=False, volume_percent=70)
    source = media.source
    source.width, source.height = 160, 90

    def tick():
        if poll == "resume":
            media.apply_startup_transport()
            return None
        return media.wait_for_video_frame(deadline=time.monotonic())

    try:
        tick()
        assert source.play_pause == source.seeks == []
        assert source.volume == 0.0 and media.state == STATE_PAUSED
        runtime.watches[-1].ready = True
        assert tick() is not True  # Native playback has not acknowledged the queued pause.
        assert source.play_pause == [True] and source.seeks == [0]
        media.set_volume(30)
        tick()
        assert source.volume == 0.0
        assert source.play_pause == [True] and source.seeks == [0]
        source.media_state = STATE_PAUSED
        result = tick()
        if poll == "video_wait":
            assert result is True
        assert source.volume == 0.3
        tick()
        assert source.play_pause == [True] and source.seeks == [0]
    finally:
        media.close()


@pytest.mark.parametrize("early_pause", ["none", "queued", "acknowledged"])
def test_visual_preparation_requires_a_frame_and_recovers_an_audio_first_pause(early_pause):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/offset.mkv", autoplay=False, volume_percent=60)
    source, watch = media.source, runtime.watches[-1]
    source._media_time = 1100  # Audio advanced; video has not published a frame.
    try:
        if early_pause != "none":
            media.apply_startup_transport()
            assert source.play_pause == [True]
        if early_pause == "acknowledged":
            source.media_state = STATE_PAUSED
            media.apply_startup_transport()
            assert source.volume == 0.6

        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert not watch.ready and source.width == source.height == 0
        assert source.volume == 0.0 and media.state == STATE_PAUSED
        assert source.play_pause == ([] if early_pause == "none" else [True, False])

        source.media_state = STATE_PLAYING  # Private preroll has resumed.
        watch.ready = True
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert source.play_pause[-1] is True and source.volume == 0.0
        source.media_state = STATE_PAUSED
        assert media.wait_for_video_frame(deadline=time.monotonic())
        assert source.volume == 0.6
    finally:
        media.close()


def test_visual_rearm_preserves_the_latest_explicit_seek_after_audio_first_pause_ack():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/offset.mkv", autoplay=False)
    source, watch = media.source, runtime.watches[-1]
    try:
        media.seek(2000)
        media.seek(1000)
        source._media_time = 1100  # Audio progress, not an operator seek.
        media.apply_startup_transport()
        assert source.seeks == [1000] and source.play_pause == [True]
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert media._startup is None and not watch.ready

        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert source.play_pause == [True, False] and source.volume == 0.0
        source.media_state = STATE_PLAYING
        watch.ready = True
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert source.seeks == [1000, 1000]
        source.media_state = STATE_PAUSED
        assert media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


def test_nonblocking_visual_demand_arms_before_decoder_update_and_waits_for_pause_ack():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/slow.mp4", autoplay=False)
    source, watch = media.source, runtime.watches[-1]
    try:
        watch.updating = True
        source.media_state = libobs_media_source.STATE_STOPPED
        media.require_video_frame()
        assert not media.video_frame_ready
        watch.updating = False
        source.media_state = STATE_PLAYING
        source._media_time = 100  # Audio alone cannot satisfy visual preparation.
        media.apply_startup_transport()
        assert source.play_pause == []
        watch.ready = True
        media.apply_startup_transport()
        assert source.play_pause == [True]
        assert not media.video_frame_ready
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert media.video_frame_ready
        media.stop()
        assert not media.video_frame_ready
    finally:
        media.close()
    assert not media.video_frame_ready


def test_audio_only_initial_pause_uses_position_without_a_video_frame():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/song.mp3", autoplay=False, volume_percent=60)
    source = media.source
    source.width = source.height = 0
    try:
        media.apply_startup_transport()
        assert source.play_pause == [] and source.volume == 0.0
        source.media_time = 40
        source.seeks.clear()  # Decoder progress is not a transport seek.
        media.apply_startup_transport()
        assert not runtime.watches[-1].ready
        assert source.play_pause == [True] and source.seeks == [0]
        assert source.volume == 0.0
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert source.volume == 0.6 and media.state == STATE_PAUSED
        assert not media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


@pytest.mark.parametrize("pause_queued", [False, True])
def test_play_cancels_initial_pause_and_restores_volume(pause_queued):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4", autoplay=False, volume_percent=65)
    source = media.source
    try:
        if pause_queued:
            runtime.watches[-1].ready = True
            media.apply_startup_transport()
        media.play()
        assert source.volume == 0.65 and media.state == STATE_PLAYING
        assert source.play_pause == ([True, False] if pause_queued else [False])
        runtime.watches[-1].ready = True
        media.apply_startup_transport()
        assert source.play_pause[-1] is False
        assert source.seeks == ([0] if pause_queued else [])
    finally:
        media.close()


def test_pause_requested_before_startup_is_deferred_and_muted():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4", volume_percent=80)
    source = media.source
    try:
        media.pause()
        assert source.play_pause == [] and source.seeks == []
        assert source.volume == 0.0 and media.state == STATE_PAUSED
        runtime.watches[-1].ready = True
        media.apply_startup_transport()
        assert source.play_pause == [True] and source.seeks == [0]
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert source.volume == 0.8
    finally:
        media.close()


@pytest.mark.parametrize("command", ["stop", "restart"])
@pytest.mark.parametrize("pause_queued", [False, True])
def test_stop_and_restart_cancel_startup_pause(command, pause_queued):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4", autoplay=False)
    source = media.source
    try:
        if pause_queued:
            runtime.watches[-1].ready = True
            media.apply_startup_transport()
        getattr(media, command)()
        source.play_pause.clear()
        source.seeks.clear()
        runtime.watches[-1].ready = True
        source.media_state = STATE_PLAYING
        source.media_time = 40
        source.seeks.clear()
        media.apply_startup_transport()
        assert source.play_pause == source.seeks == []
        if command == "stop":
            assert media.state == libobs_media_source.STATE_STOPPED
            assert source.stops == 1 and source.volume == 0.0
            source.media_state = libobs_media_source.STATE_STOPPED
            media.apply_startup_transport()
            assert media._startup is None and source.volume == 1.0
        else:
            assert media.state == STATE_PLAYING
        assert runtime.watches[-1].invalidations == 1
    finally:
        media.close()


def test_speed_change_preserves_initial_pause_until_the_decoder_is_ready():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("https://cdn.example/clip.mp4", autoplay=False, volume_percent=70)
    source = media.source
    try:
        media.set_speed(150)
        media.apply_startup_transport()
        assert source.play_pause == source.seeks == []
        assert media.state == STATE_PAUSED and source.volume == 0.0
        assert runtime.watches[-1].invalidations == 0
        runtime.watches[-1].ready = True
        media.apply_startup_transport()
        assert source.play_pause == [True] and source.seeks == [0]
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert source.volume == 0.7
    finally:
        media.close()


def test_hardware_decode_requested_by_default(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    runtime = _Runtime()
    LibobsMediaSource(runtime).open("/tmp/clip.mp4")
    # ffmpeg_source decodes on the GPU when possible, software per-stream otherwise.
    assert runtime.created[-1].settings["hw_decode"] is True


def test_hardware_decode_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SOLIN_MEDIA_HW_DECODE", "0")
    runtime = _Runtime()
    LibobsMediaSource(runtime).open("/tmp/clip.mp4")
    assert runtime.created[-1].settings["hw_decode"] is False


def test_transport_and_state():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    runtime.watches[-1].ready = True
    media.apply_startup_transport()
    source.play_pause.clear()

    media.pause()
    media.play()
    assert source.play_pause == [True, False]

    media.seek(1500)
    assert source.media_time == 1500

    media.restart()
    assert source.restarts == 1

    source.media_time = 4200
    source.media_duration = 9000
    source.media_state = STATE_PLAYING
    assert media.position_ms == 4200
    assert media.duration_ms == 9000
    assert media.state == STATE_PLAYING


def test_reopen_releases_the_previous_source():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/a.mp4")
    first = runtime.created[-1]

    media.open("/tmp/b.mp4")

    assert first.stops == 1
    assert first.released == 1
    assert runtime.watches[0].closed and not runtime.watches[1].closed
    assert media.path == "/tmp/b.mp4"


def test_close_stops_and_releases():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]

    media.close()

    assert source.stops == 1
    assert source.released == 1
    assert runtime.watches[-1].closed
    assert source.lifecycle[-3:] == ["unwatch", "stop", "release"]
    assert media.source is None
    assert media.state == STATE_NONE


def test_decoding_does_not_wait_for_libobs_to_activate_the_source(monkeypatch):
    """Media shown only on projection/preview must still decode.

    ffmpeg_source starts playback from its `activate` callback, which fires only for
    sources on an output channel (the MAIN view). Projection, the editor preview and
    the scene-card thumbnails hold show refs instead, so an activation-gated source
    sits frozen at 0 ms on them. Solin owns the transport, so it opts out.
    """
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    runtime = _Runtime()

    LibobsMediaSource(runtime).open("/tmp/clip.mp4")

    assert runtime.created[-1].settings["restart_on_activate"] is False


class _ActivationRecorder:
    """Stands in for the unwrapped libobs activation symbols."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def obs_source_inc_active(self, pointer) -> None:
        self.calls.append(("inc", pointer))

    def obs_source_dec_active(self, pointer) -> None:
        self.calls.append(("dec", pointer))


def _with_activation(monkeypatch):
    import pylibobs._ffi as ffi_module

    recorder = _ActivationRecorder()
    monkeypatch.setattr(ffi_module, "get_lib", lambda: recorder)
    return recorder


@pytest.fixture(autouse=True)
def _isolate_native_activation(monkeypatch):
    """Fake sources must never load the real libobs library into Qt unit tests."""
    _with_activation(monkeypatch)


def test_open_media_holds_an_activate_ref_so_it_can_be_heard(monkeypatch):
    """Monitored audio is dropped while activate_refs is zero.

    libobs' monitoring backends bail out when the source is not active, and only the
    MAIN view raises that counter — projection, the editor preview and the scene
    cards all take show refs instead. Without an explicit activate ref a song routed
    anywhere but the virtual camera decoded silently.
    """
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    recorder = _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("/tmp/song.mp3") is True

    source = runtime.created[-1]
    assert recorder.calls == [("inc", source._ptr)]

    media.close()

    assert recorder.calls == [("inc", source._ptr), ("dec", source._ptr)]


def test_the_activate_ref_is_taken_once_and_released_once(monkeypatch):
    """A leaked or doubled ref would strand the source active for the session."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    recorder = _with_activation(monkeypatch)
    media = LibobsMediaSource(_Runtime())

    media.open("/tmp/song.mp3")
    media.close()
    media.close()  # idempotent: no second dec

    assert [kind for kind, _ in recorder.calls] == ["inc", "dec"]


@pytest.mark.parametrize("path", ["/clip.mp4", "https://cdn.example/clip.mp4"])
def test_detach_presentation_unloads_transport_without_discarding_video(path, monkeypatch):
    recorder = _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open(path)
    source = runtime.created[-1]

    assert media.detach_presentation() is source
    assert source.volume == 0.0
    assert source.play_pause[-1] is True
    assert source.settings["is_local_file"] is True
    assert source.settings["local_file"] == source.settings["input"] == ""
    assert source.settings["clear_on_media_end"] is False
    assert source.stops == source.released == 0
    assert media.source is None and media.path == ""
    assert runtime.watches[-1].closed
    assert source.lifecycle[-1] == "unwatch"
    assert [kind for kind, _ in recorder.calls] == ["inc", "dec"]
    assert media.detach_presentation() is None
    media.close()
    assert source.stops == source.released == 0
    source.release()  # ownership was transferred to the presentation owner


def test_failed_presentation_pause_closes_the_source_instead_of_orphaning_it(monkeypatch):
    _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/clip.mp4")
    source = runtime.created[-1]

    def fail_pause(_pause):
        raise RuntimeError("pause failed")

    source.media_play_pause = fail_pause
    with pytest.raises(RuntimeError, match="pause failed"):
        media.detach_presentation()
    assert media.source is None
    assert source.stops == source.released == 1


def test_failed_input_unload_transfers_the_borrowed_picture_without_stopping_it(monkeypatch, caplog):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/clip.mp4")
    source = runtime.created[-1]

    def fail_update(_settings):
        raise RuntimeError("update failed")

    monkeypatch.setattr(source, "update", fail_update)
    assert media.detach_presentation() is source
    assert media.source is None
    assert source.volume == 0.0 and source.play_pause[-1] is True
    assert source.stops == source.released == 0
    assert "Could not unload retired media input" in caplog.text
    source.release()


def test_a_wrapper_without_a_raw_pointer_does_not_break_playback(monkeypatch):
    """The activation call is best-effort; media must still open without it."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    _with_activation(monkeypatch)
    runtime = _Runtime()
    original = _FakeMediaSource.__init__

    def without_pointer(self, kind, name, settings):
        original(self, kind, name, settings)
        del self._ptr

    monkeypatch.setattr(_FakeMediaSource, "__init__", without_pointer)
    media = LibobsMediaSource(runtime)

    assert media.open("/tmp/song.mp3") is True
    media.close()


def test_progressive_http_uses_file_lifecycle_and_position_preserving_recovery(monkeypatch):
    """Network location must not turn finite EOF into a live reconnection."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    runtime = _Runtime()

    LibobsMediaSource(runtime).open("https://cdn.example/song.mp3")

    settings = runtime.created[-1].settings
    assert settings["is_local_file"] is True
    assert settings["local_file"] == "https://cdn.example/song.mp3"
    # A dropped packet must be recovered below av_read_frame: media-playback ends
    # its decode thread on any read error, which surfaces as end-of-media.
    assert "reconnect=1" in settings["ffmpeg_options"]
    assert "reconnect_on_network_error=1" in settings["ffmpeg_options"]
    # Bound stalled HTTP reads as well as retry backoff.
    assert "rw_timeout=" in settings["ffmpeg_options"]
    assert "reconnect_delay_sec" not in settings


def test_live_transports_are_not_marked_seekable(monkeypatch):
    """rtsp/rtmp/srt cannot seek and do need the network interrupt handling."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)

    for url in ("rtsp://cam.local/stream", "rtmp://server/live", "srt://host:9000"):
        runtime = _Runtime()
        LibobsMediaSource(runtime).open(url)
        settings = runtime.created[-1].settings
        assert settings["is_local_file"] is False
        assert settings["input"] == url
        assert "seekable" not in settings, url
        assert "ffmpeg_options" not in settings, url


def test_local_files_are_left_alone(monkeypatch):
    """A local file is already seekable; it must not take the network options."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    runtime = _Runtime()

    LibobsMediaSource(runtime).open("/tmp/clip.mp4")

    settings = runtime.created[-1].settings
    assert "seekable" not in settings
    assert "ffmpeg_options" not in settings


@pytest.mark.parametrize("path", ["/tmp/clip.mp4", "https://cdn.example/video.mp4", "rtsp://camera/live"])
@pytest.mark.parametrize("state", [STATE_PLAYING, STATE_PAUSED])
def test_rate_changes_preserve_source_transport_and_visual_readiness(path, state):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open(path, volume_percent=70)
    source = media.source
    watch = runtime.watches[-1]
    watch.ready = True
    media.apply_startup_transport()
    source.media_state = state
    source.media_time = 42_000
    source.seeks.clear()
    source.updates.clear()
    try:
        assert media.video_frame_ready
        media.set_speed(150)
        media.set_speed(150)
        for _ in range(12):
            media.apply_startup_transport()
        assert media.source is source and len(runtime.created) == 1
        assert source.updates == [{"speed_percent": 150}]
        assert source.media_time == 42_000 and source.seeks == []
        assert source.media_state == state and source.play_pause == []
        assert source.stops == source.restarts == source.released == 0
        assert media.video_frame_ready and watch.invalidations == 0
        assert source.volume == 0.7
    finally:
        media.close()


@pytest.mark.parametrize("startup_phase", ["complete", "decoding", "pause_queued"])
def test_rejected_rate_update_preserves_transport_and_can_be_retried(monkeypatch, startup_phase):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4", autoplay=False, volume_percent=70)
    source = media.source
    watch = runtime.watches[-1]
    if startup_phase != "decoding":
        watch.ready = True
        media.apply_startup_transport()
        if startup_phase == "complete":
            source.media_state = STATE_PAUSED
            media.apply_startup_transport()
    original_update = source.update
    def reject(_settings):
        raise RuntimeError("queue rejected")
    monkeypatch.setattr(source, "update", reject)
    media.set_speed(150)
    assert source.settings.get("speed_percent", 100) == 100
    assert media.state == STATE_PAUSED
    assert source.volume == (0.7 if startup_phase == "complete" else 0.0)
    assert watch.ready is (startup_phase != "decoding")
    monkeypatch.setattr(source, "update", original_update)
    media.set_speed(150)
    assert source.settings["speed_percent"] == 150
    media.close()


@pytest.mark.parametrize("command", ["close", "detach_presentation"])
def test_failed_readiness_close_retains_both_owners_for_explicit_retry(monkeypatch, command):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/clip.mp4")
    source = media.source
    watch = media._video_readiness
    original_close = watch.close

    def reject():
        raise RuntimeError("callback removal failed")

    monkeypatch.setattr(watch, "close", reject)
    with pytest.raises(RuntimeError, match="callback removal failed"):
        getattr(media, command)()
    assert media.source is source and media._video_readiness is watch
    assert source.stops == source.released == 0
    monkeypatch.setattr(watch, "close", original_close)
    result = getattr(media, command)()
    assert media.source is None and media._video_readiness is None and watch.closed
    if command == "detach_presentation":
        assert result is source and source.released == 0
        source.release()  # The caller now owns the retired presentation.
    else:
        assert result is None and source.stops == source.released == 1
    media.close()
    assert source.released == 1


@pytest.mark.parametrize("url", ["rtsp://camera/live", "rtmp://server/live", "srt://host:9000"])
def test_nonseekable_startup_ignores_trim_without_stranding_requested_pause(url):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open(url, autoplay=False)
    source = media.source
    try:
        media.seek(2000)
        assert media._requested_seek_ms == 0
        runtime.watches[-1].ready = True
        media.apply_startup_transport()
        assert source.play_pause == [True] and source.seeks == [0]
        source.media_state = STATE_PAUSED
        media.apply_startup_transport()
        assert media._startup is None and media.state == STATE_PAUSED
        assert media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()
