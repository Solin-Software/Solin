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


def test_open_local_file_creates_ffmpeg_source_and_autoplays():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("/tmp/clip.mp4", autoplay=True) is True

    source = runtime.created[-1]
    assert source.kind == "ffmpeg_source"
    assert source.settings == {"is_local_file": True, "local_file": "/tmp/clip.mp4"}
    assert source.play_pause == [False]  # autoplay → not paused
    assert runtime.monitored == [(source, runtime.monitored[0][1])]  # monitoring set
    assert media.source is source
    assert media.path == "/tmp/clip.mp4"


def test_open_remote_url_uses_the_input_setting_and_can_start_paused():
    runtime = _Runtime()
    media = LibobsMediaSource(runtime)

    assert media.open("https://cdn.example/v.mp4", autoplay=False) is True

    source = runtime.created[-1]
    assert source.settings == {"is_local_file": False, "input": "https://cdn.example/v.mp4"}
    assert source.play_pause == [True]  # paused


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
