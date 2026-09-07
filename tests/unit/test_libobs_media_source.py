"""Unit tests for the Qt-free libobs media source (ffmpeg_source wrapper)."""

from __future__ import annotations

import types

from solin.core.scenes.libobs_media_source import (
    STATE_NONE,
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

    def release(self) -> None:
        self.released += 1


class _Runtime:
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
