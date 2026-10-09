"""Unit tests for the Qt-free libobs media source (ffmpeg_source wrapper)."""

from __future__ import annotations

import types
import time

import pytest

from solin.core.scenes import libobs_media_source
from solin.core.scenes.libobs_media_source import (
    _PROGRESSIVE_FFMPEG_OPTIONS,
    _RESUME_REBUILD_TICKS,
    STATE_NONE,
    STATE_OPENING,
    STATE_PAUSED,
    STATE_PLAYING,
    LibobsMediaSource,
)


class _FakeMediaSource:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self._ptr = object()  # stands in for the raw obs_source_t*
        self.play_pause: list[bool] = []
        self.stops = 0
        self.restarts = 0
        self.updates: list[dict] = []
        self.released = 0
        self.media_time = 0
        self.media_duration = 0
        self.media_state = STATE_PLAYING

    def media_play_pause(self, pause: bool) -> None:
        self.play_pause.append(pause)

    def media_stop(self) -> None:
        self.stops += 1

    def media_restart(self) -> None:
        self.restarts += 1

    def update(self, settings: dict) -> None:
        self.updates.append(dict(settings))
        self.settings.update(settings)

    def release(self) -> None:
        self.released += 1


class _Runtime:
    @staticmethod
    def prime_source_video(source, *, allow_preloaded):
        return source.width > 0 and source.height > 0

    def __init__(self) -> None:
        self.created: list[_FakeMediaSource] = []
        self.monitored: list = []
        runtime = self

        class _SourceNS:
            @staticmethod
            def create(kind: str, name: str, settings: dict) -> _FakeMediaSource:
                source = _FakeMediaSource(kind, name, settings)
                runtime.created.append(source)
                return source

        self.ob = types.SimpleNamespace(Source=_SourceNS)

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
    try:
        assert not media.wait_for_video_frame(deadline=time.monotonic() + 10)
    finally:
        media.close()


def test_visual_readiness_waits_for_gpu_priming_and_reuses_the_primed_source(monkeypatch):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("/video.mp4")
    source = media.source
    source.width, source.height = 160, 90
    calls = []
    ready = False

    def prime(candidate, *, allow_preloaded):
        assert candidate is source
        calls.append(allow_preloaded)
        return ready

    monkeypatch.setattr(runtime, "prime_source_video", prime)
    try:
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        ready = True
        assert media.wait_for_video_frame(deadline=time.monotonic())
        assert media.wait_for_video_frame(deadline=time.monotonic())
        assert calls == [False, False]
        source.media_state = libobs_media_source.STATE_ERROR
        assert not media.wait_for_video_frame(deadline=time.monotonic())
    finally:
        media.close()


@pytest.mark.parametrize("change", ["stop", "restart", "speed"])
def test_decoder_reset_invalidates_priming_even_when_the_source_pointer_is_unchanged(change, monkeypatch):
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    assert media.open("https://cdn.example/video.mp4")
    source = media.source
    source.width, source.height = 160, 90
    try:
        assert media.wait_for_video_frame(deadline=time.monotonic())
        if change == "speed":
            media.set_speed(150)
        else:
            getattr(media, change)()
        source.media_state = STATE_PLAYING
        primed = []
        monkeypatch.setattr(runtime, "prime_source_video", lambda *_args, **_kwargs: primed.append(True) or False)
        assert media.source is source
        assert not media.wait_for_video_frame(deadline=time.monotonic())
        assert primed == [True]
    finally:
        media.close()


def test_open_local_file_creates_ffmpeg_source_and_autoplays(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)  # default: HW on
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("/tmp/clip.mp4", autoplay=True) is True

    source = runtime.created[-1]
    assert source.kind == "ffmpeg_source"
    assert source.settings == {
        "is_local_file": True, "local_file": "/tmp/clip.mp4", "hw_decode": True,
        "restart_on_activate": False,
    }
    assert source.play_pause == [False]  # autoplay → not paused
    assert runtime.monitored == [(source, runtime.monitored[0][1])]  # monitoring set
    assert media.source is source
    assert media.path == "/tmp/clip.mp4"


def test_open_remote_url_uses_the_input_setting_and_can_start_paused(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)  # default: HW on
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("https://cdn.example/v.mp4", autoplay=False) is True

    source = runtime.created[-1]
    assert source.settings == {
        "is_local_file": False, "input": "https://cdn.example/v.mp4", "hw_decode": True,
        "restart_on_activate": False,
        # http(s) origins serve byte ranges, so the stream is seekable
        "seekable": True,
        "ffmpeg_options": _PROGRESSIVE_FFMPEG_OPTIONS,
        "reconnect_delay_sec": 2,
    }
    assert source.play_pause == [True]  # paused


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
    assert media.path == "/tmp/b.mp4"


def test_close_stops_and_releases():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]

    media.close()

    assert source.stops == 1
    assert source.released == 1
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


def test_detach_presentation_relinquishes_transport_without_discarding_video(monkeypatch):
    recorder = _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/clip.mp4")
    source = runtime.created[-1]
    tempo_stops = []
    media._tempo_audio.stop = lambda: tempo_stops.append(True)

    assert media.detach_presentation() is source
    assert source.volume == 0.0
    assert source.play_pause[-1] is True
    assert source.stops == source.released == 0
    assert media.source is None and media.path == ""
    assert tempo_stops == [True]
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


def test_progressive_http_media_is_marked_seekable(monkeypatch):
    """Without this the seek is accepted, queued, and silently dropped.

    ffmpeg_source composes media-playback's is_local_file as
    ``is_local_file || seekable``, and media-playback wraps av_seek_frame in
    ``if (m->is_local_file)``. A streamed URL with neither flag therefore ignores
    every seek while the position keeps running, so the slider snaps back.
    """
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    runtime = _Runtime()

    LibobsMediaSource(runtime).open("https://cdn.example/song.mp3")

    settings = runtime.created[-1].settings
    assert settings["seekable"] is True
    # A dropped packet must be recovered below av_read_frame: media-playback ends
    # its decode thread on any read error, which surfaces as end-of-media.
    assert "reconnect=1" in settings["ffmpeg_options"]
    assert "reconnect_on_network_error=1" in settings["ffmpeg_options"]
    # Marking it seekable drops the socket interrupt callback, so cap the read.
    assert "rw_timeout=" in settings["ffmpeg_options"]
    # And if ffmpeg gives up, obs rebuilds far sooner than its 10 s default.
    assert settings["reconnect_delay_sec"] == 2


def test_live_transports_are_not_marked_seekable(monkeypatch):
    """rtsp/rtmp/srt cannot seek and do need the network interrupt handling."""
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)

    for url in ("rtsp://cam.local/stream", "rtmp://server/live", "srt://host:9000"):
        runtime = _Runtime()
        LibobsMediaSource(runtime).open(url)
        settings = runtime.created[-1].settings
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


def test_an_unchanged_rate_never_reaches_the_source(monkeypatch):
    """Volume travels on the same message, and a stream restarts on any update.

    ffmpeg_source sets should_restart_media unconditionally for a non-local input,
    so re-sending the rate the source already has was enough to send a streamed
    video back to the beginning every time the volume slider moved.
    """
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("https://cdn.example/v.mp4", speed_percent=100)
    source = runtime.created[-1]
    source.updates.clear()

    media.set_speed(100)  # what a volume change re-sends

    assert source.updates == []


def test_a_real_rate_change_is_applied(monkeypatch):
    monkeypatch.delenv("SOLIN_MEDIA_HW_DECODE", raising=False)
    _with_activation(monkeypatch)
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.updates.clear()

    _change_speed(media, 150)

    assert source.updates == [{"speed_percent": 150}]
    # …and asking for it twice does not update twice.
    media.set_speed(150)
    assert source.updates == [{"speed_percent": 150}]


def test_a_rate_change_restores_the_position_once_the_decoder_is_back():
    """The rate is baked into the media object, so a change replays from zero.

    The seek cannot ride along with the update: ffmpeg_source is an async source,
    so obs_source_update only defers, and a seek issued straight away would land on
    the media object that is about to be destroyed.
    """
    for url in ("https://cdn.example/v.mp4", "/tmp/clip.mp4"):
        runtime = _Runtime()
        media = LibobsMediaSource(runtime)
        media.open(url)
        source = runtime.created[-1]
        source.media_time = 42_000

        media.set_speed(150)

        assert source.media_time == 42_000, f"{url}: not seeked yet"
        source.media_time = 0  # what the rebuilt decoder reports
        media.apply_pending_resume()
        assert source.media_time == 42_000, f"{url}: position not restored"


def test_the_resume_waits_while_the_decoder_is_still_opening():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.media_time = 30_000
    media.set_speed(150)
    source.media_time = 0
    source.media_state = STATE_OPENING

    media.apply_pending_resume()
    assert source.media_time == 0, "must not seek a decoder that is still opening"

    source.media_state = STATE_PLAYING
    media.apply_pending_resume()
    assert source.media_time == 30_000


def test_a_paused_source_stays_paused_across_a_rate_change():
    """ffmpeg_source force-resumes on restart; the operator did not ask for that."""
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.media_time = 5_000
    source.media_state = STATE_PAUSED

    media.set_speed(150)
    source.media_state = STATE_PLAYING  # the restart resumed it
    source.media_time = 0
    media.apply_pending_resume()

    assert source.play_pause[-1] is True, "must be paused again"


def test_a_poll_before_the_rebuild_does_not_spend_the_restore():
    """The media poll runs four times a second, so it lands in the gap.

    ffmpeg_source does not swap its media object on the tick that asks for a new
    rate: for a moment the old one still answers, reporting the very position being
    preserved. Seeking then is a no-op that the restart goes on to undo, so a
    restore counted as done there leaves playback at the start of the item.
    """
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.media_time = 42_000

    media.set_speed(150)
    media.apply_pending_resume()  # the old media object is still answering
    source.media_time = 0  # now the rebuilt decoder appears
    media.apply_pending_resume()

    assert source.media_time == 42_000


def test_a_decoder_that_keeps_its_place_is_not_waited_on_for_ever():
    """Nothing guarantees a restart; the restore has to settle either way."""
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.media_time = 8_000

    media.set_speed(150)
    for _ in range(_RESUME_REBUILD_TICKS + 2):
        source.media_time += 400  # it carried on from where it was
        media.apply_pending_resume()

    assert media._resume_ms == 0, "the restore must not stay armed for ever"
    assert source.media_time > 8_000, "and must not drag playback backwards"


def test_the_resume_is_applied_only_once():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    media.open("/tmp/clip.mp4")
    source = runtime.created[-1]
    source.media_time = 9_000
    media.set_speed(150)
    source.media_time = 0

    media.apply_pending_resume()
    source.media_time = 500  # playback moved on
    media.apply_pending_resume()

    assert source.media_time == 500, "a second poll must not seek again"


# ── a dropped stream must not look like the end of the video ─────────────────


def _sidecar():
    from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine

    sidecar = LibobsSidecarEngine.__new__(LibobsSidecarEngine)
    sidecar._media_last_progress = {}
    sidecar._media_trim_ended = False
    return sidecar


def test_a_stream_ending_far_from_the_end_is_reported_as_buffering():
    """media-playback reports any read error as ENDED, like a finished video.

    Taken at face value the app advances the playlist, so a moment of bad wifi
    skipped to the next item in the middle of a meeting.
    """
    from solin.core.scenes.libobs_media_source import STATE_BUFFERING, STATE_ENDED, STATE_PLAYING

    sidecar = _sidecar()
    sidecar._survive_disconnect(0, STATE_PLAYING, 30_000, 140_000, "https://cdn/x.mp4")

    state, position, duration = sidecar._survive_disconnect(
        0, STATE_ENDED, 0, 0, "https://cdn/x.mp4"
    )

    assert state == STATE_BUFFERING
    assert (position, duration) == (30_000, 140_000), "hold the last known position"


def test_a_stream_that_really_finished_still_ends():
    from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PLAYING

    sidecar = _sidecar()
    sidecar._survive_disconnect(0, STATE_PLAYING, 139_000, 140_000, "https://cdn/x.mp4")

    state, _position, _duration = sidecar._survive_disconnect(
        0, STATE_ENDED, 139_500, 140_000, "https://cdn/x.mp4"
    )

    assert state == STATE_ENDED


def test_a_local_file_ending_early_is_left_alone():
    """Only a network read can drop; a local file ending early means something else."""
    from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PLAYING

    sidecar = _sidecar()
    sidecar._survive_disconnect(0, STATE_PLAYING, 10_000, 140_000, "/tmp/clip.mp4")

    state, _p, _d = sidecar._survive_disconnect(0, STATE_ENDED, 0, 0, "/tmp/clip.mp4")

    assert state == STATE_ENDED


def test_a_trimmed_end_still_ends():
    """The trim window latches ENDED deliberately; do not second-guess it."""
    from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PLAYING

    sidecar = _sidecar()
    sidecar._survive_disconnect(0, STATE_PLAYING, 5_000, 140_000, "https://cdn/x.mp4")
    sidecar._media_trim_ended = True

    state, _p, _d = sidecar._survive_disconnect(0, STATE_ENDED, 5_000, 140_000, "https://cdn/x.mp4")

    assert state == STATE_ENDED


# ── pitch-preserving speed changes ───────────────────────────────────────────


class _FakeCompanion:
    """Records what the media source asks of the stretched-audio companion."""

    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.stops = 0
        self.volumes: list[int] = []
        self.paused: list[bool] = []
        self.available = True
        self.running = False
        self.flow_delay_ms = 0  # how much audio plays before obs emits it

    def start(self, path, *, position_ms, rate, volume_percent=100, paused=False) -> bool:
        self.starts.append(
            {"path": path, "position_ms": position_ms, "rate": rate,
             "volume_percent": volume_percent, "paused": paused}
        )
        self.running = self.available
        return self.available

    def await_audio(self, timeout_s: float = 3.0) -> int:
        return self.flow_delay_ms

    @property
    def elapsed_ms(self) -> int:
        return self.flow_delay_ms

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    def set_volume(self, volume_percent: int) -> None:
        self.volumes.append(volume_percent)

    def set_paused(self, paused: bool) -> None:
        self.paused.append(paused)


def _opened(path="/tmp/clip.mp4", **kwargs):
    """A media source open on ``path`` with a fake companion in place."""
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)
    companion = _FakeCompanion()
    media._tempo_audio = companion
    assert media.open(path, **kwargs) is True
    return media, runtime, companion


# What the rebuilt decoder's position moves by once it really starts running.
_RESUME_STEP_MS = 40


@pytest.fixture(autouse=True)
def _dont_wait_for_a_fake_decoder(monkeypatch):
    """These fakes never advance on their own; waiting only slows the suite down.

    The wait itself is covered by test_the_audio_is_released_when_the_picture_moves.
    """
    monkeypatch.setattr(libobs_media_source, "_MOTION_TIMEOUT_S", 0.0)


def _change_speed(media, percent: int, *, restarts: bool = True) -> None:
    """Change the rate and run the poll ticks the sidecar would run next.

    ffmpeg_source rebuilds its media object for a new rate and the rebuilt one
    starts from zero — but not on the tick that asked for it, so the first tick
    still sees the old position.
    """
    media.set_speed(percent)
    source = media._source
    media.apply_pending_resume()  # the old media object is still answering
    if source is None:
        return
    if restarts:
        source.media_time = 0  # the rebuilt decoder appears...
        media.apply_pending_resume()  # ...and is put back where it was
    source.media_time += _RESUME_STEP_MS  # it starts running again
    media.apply_pending_resume()  # the stretch can be opened now


def test_a_speed_change_stretches_the_audio_instead_of_raising_its_pitch():
    media, runtime, companion = _opened()
    runtime.created[-1].media_time = 42_000

    _change_speed(media, 150)

    assert companion.starts[-1]["rate"] == 1.5
    assert companion.starts[-1]["path"] == "/tmp/clip.mp4"


def test_the_stretch_picks_up_from_where_playback_had_reached():
    media, runtime, companion = _opened()
    runtime.created[-1].media_time = 42_000

    _change_speed(media, 150)

    assert companion.starts[-1]["position_ms"] == pytest.approx(42_000, abs=100)


def test_the_source_is_muted_while_the_companion_carries_the_audio():
    """Otherwise both the varispeeded and the stretched audio would be heard."""
    media, runtime, _companion = _opened()

    _change_speed(media, 150)

    assert runtime.created[-1].volume == 0.0


def test_returning_to_normal_speed_stops_the_stretch_and_unmutes():
    media, runtime, companion = _opened(volume_percent=80)
    _change_speed(media, 150)

    _change_speed(media, 100)

    assert companion.stops >= 1
    assert companion.running is False
    assert runtime.created[-1].volume == 0.8


def test_a_companion_that_cannot_start_leaves_the_audio_audible():
    """Falling back to varispeed is worse than pitch-perfect, better than silence."""
    media, runtime, companion = _opened(volume_percent=60)
    companion.available = False

    _change_speed(media, 150)

    assert runtime.created[-1].volume == 0.6


def test_volume_changes_reach_the_companion_and_keep_the_source_muted():
    media, runtime, companion = _opened()
    _change_speed(media, 150)

    media.set_volume(30)

    assert companion.volumes[-1] == 30
    assert runtime.created[-1].volume == 0.0


def test_pausing_and_resuming_carry_the_stretched_audio_with_them():
    media, _runtime, companion = _opened()
    _change_speed(media, 150)

    media.pause()
    media.play()

    assert companion.paused[-2:] == [True, False]


def test_seeking_restarts_the_stretch_at_the_new_position():
    """The stretch is a separate decode, so it has to be re-cut at the seek point."""
    media, _runtime, companion = _opened()
    _change_speed(media, 150)

    media.seek(90_000)

    assert companion.starts[-1]["position_ms"] == 90_000


def test_closing_the_media_takes_the_stretch_down_with_it():
    media, _runtime, companion = _opened()
    _change_speed(media, 150)

    media.close()

    assert companion.stops >= 1
    assert companion.running is False


def test_media_opened_at_a_faster_rate_is_stretched_from_the_start():
    _media, _runtime, companion = _opened(speed_percent=150)

    assert companion.starts[-1]["rate"] == 1.5
    assert companion.starts[-1]["position_ms"] == 0


def test_remote_media_keeps_varispeed_rather_than_downloading_it_twice():
    media, runtime, companion = _opened("https://example.org/talk.mp4", volume_percent=70)

    _change_speed(media, 150)

    assert companion.starts == []
    assert runtime.created[-1].volume == 0.7  # audible, just varispeeded


def test_setting_the_same_speed_again_does_not_restart_the_stretch():
    media, _runtime, companion = _opened()
    _change_speed(media, 150)

    _change_speed(media, 150)

    assert len(companion.starts) == 1


def test_the_stretch_waits_for_the_rebuilt_decoder_before_it_starts():
    """Started at set_speed time, the audio would run ahead of the winding-back picture."""
    media, _runtime, companion = _opened()

    media.set_speed(150)

    assert companion.starts == []


def test_the_source_stays_silent_over_the_gap_rather_than_blipping_in_pitch():
    media, runtime, _companion = _opened(volume_percent=90)

    media.set_speed(150)

    assert runtime.created[-1].volume == 0.0


def test_the_stretch_waits_for_the_decoder_to_actually_start_moving():
    """It reports PLAYING for a second or two first; audio opened then runs ahead."""
    media, runtime, companion = _opened()
    source = runtime.created[-1]
    source.media_time = 42_000

    media.set_speed(150)
    media.apply_pending_resume()  # PLAYING, but the position has not budged
    assert companion.starts == []

    source.media_time = 0  # the rebuilt decoder appears
    media.apply_pending_resume()  # and is put back to 42_000
    assert companion.starts == []

    source.media_time = 42_040  # now it is really running
    media.apply_pending_resume()

    assert companion.starts[-1]["position_ms"] == 42_040


def test_a_stretch_that_never_starts_gives_the_audio_straight_back():
    media, runtime, companion = _opened(volume_percent=90)
    companion.available = False

    media.set_speed(150)
    assert runtime.created[-1].volume == 0.0  # silent while it is being tried
    _change_speed(media, 150)  # the decoder comes back and the attempt is made

    assert runtime.created[-1].volume == 0.9


def test_a_rate_change_while_the_decoder_rebuilds_is_retried_next_tick():
    media, runtime, companion = _opened()
    source = runtime.created[-1]
    media.set_speed(150)
    source.media_state = STATE_OPENING

    media.apply_pending_resume()
    assert companion.starts == []

    source.media_state = STATE_PLAYING
    source.media_time = 0
    media.apply_pending_resume()
    source.media_time += _RESUME_STEP_MS
    media.apply_pending_resume()
    assert len(companion.starts) == 1


def test_the_picture_is_held_still_while_the_stretch_is_opened():
    """Left running, the video would end up seconds ahead of the audio for good."""
    media, runtime, _companion = _opened()
    source = runtime.created[-1]
    source.play_pause.clear()

    _change_speed(media, 150)

    assert source.play_pause == [True, False]


def test_the_picture_skips_the_audio_that_played_before_it_was_audible():
    media, runtime, companion = _opened()
    source = runtime.created[-1]
    source.media_time = 20_000
    companion.flow_delay_ms = 400  # obs buffered for 400 ms before emitting

    _change_speed(media, 150)

    resumed_at = 20_000 + _RESUME_STEP_MS
    assert source.media_time == resumed_at + int(400 * 1.5)


def test_a_paused_media_gets_a_paused_stretch_and_stays_paused():
    media, runtime, companion = _opened(autoplay=False)
    source = runtime.created[-1]
    source.media_state = STATE_PAUSED
    source.play_pause.clear()

    _change_speed(media, 150)

    assert companion.paused[-1] is True
    assert False not in source.play_pause  # never nudged back into playing


def test_the_audio_is_held_until_the_picture_actually_moves(monkeypatch):
    """Released earlier, it would run on alone while the decoder is still resuming."""
    monkeypatch.setattr(libobs_media_source, "_MOTION_TIMEOUT_S", 2.0)
    monkeypatch.setattr(libobs_media_source, "_MOTION_STEP_S", 0.001)
    media, _runtime, _companion = _opened()
    reads = {"n": 0}

    def position(_self):
        reads["n"] += 1
        return 10_000 if reads["n"] < 5 else 10_400

    monkeypatch.setattr(type(media), "position_ms", property(position))

    media._await_motion(10_000)

    assert reads["n"] >= 5  # it waited for movement rather than assuming it


def test_it_gives_up_on_a_picture_that_never_moves(monkeypatch):
    """A stalled decoder must not leave the audio held for ever."""
    monkeypatch.setattr(libobs_media_source, "_MOTION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(libobs_media_source, "_MOTION_STEP_S", 0.001)
    media, _runtime, _companion = _opened()
    monkeypatch.setattr(type(media), "position_ms", property(lambda _s: 10_000))

    media._await_motion(10_000)  # returns instead of hanging


def test_resuming_lets_the_picture_lead_before_the_audio_follows():
    """Released together, the audio would win by the decoder's resume delay."""
    media, runtime, companion = _opened()
    source = runtime.created[-1]
    _change_speed(media, 150)
    source.media_state = STATE_PAUSED
    companion.paused.clear()

    media.play()

    assert companion.paused == [False]
    assert source.play_pause[-1] is False
