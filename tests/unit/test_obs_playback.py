"""Unit tests for the libobs-backed playback engine (ObsMediaController).

libobs is fully mocked: a fake runtime hands out fake ffmpeg_source objects, so
these tests exercise the engine's own logic (source wiring, transport, the
poll → signal mapping, the audio_output shim, trim gating and the download
switch-to-local) without any real OBS context, GPU or display.
"""

from __future__ import annotations

import sys
import types

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

import solin.core.media.obs_playback as obs_playback
from solin.core.media.cache import MediaCacheManager
from solin.core.media.obs_playback import ObsMediaController
from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY
from solin.core.media.playback_request import (
    MediaPlaybackRequest,
    MediaTrim,
    PlaybackCachePolicy,
)
from solin.core.media.playback_state import PlaybackState


# ── libobs fakes ─────────────────────────────────────────────────────────────


class _FakeSource:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.media_time = 0
        self.media_duration = 0
        self.media_state = 1  # OBS "playing"
        self.volume = 1.0
        self.muted = False
        self.play_pause_calls: list[bool] = []
        self.stopped = 0
        self.restarted = 0
        self.released = 0
        self.updates: list[dict] = []

    def media_play_pause(self, pause: bool) -> None:
        self.play_pause_calls.append(pause)

    def media_stop(self) -> None:
        self.stopped += 1

    def media_restart(self) -> None:
        self.restarted += 1

    def release(self) -> None:
        self.released += 1

    def update(self, settings) -> None:
        self.updates.append(dict(settings))


class _FakeSourceFactory:
    def __init__(self, registry: list[_FakeSource]) -> None:
        self._registry = registry

    def create(self, kind: str, name: str, settings: dict) -> _FakeSource:
        source = _FakeSource(kind, name, settings)
        self._registry.append(source)
        return source


class _VideoFormat:
    BGRA = 5


class _BoundsType:
    SCALE_INNER = 2


class _Alignment:
    CENTER = 0


class _FakeSceneItem:
    def __init__(self, source) -> None:
        self.source = source
        self.bounds_type = 0
        self.bounds = (0.0, 0.0)
        self.bounds_alignment = -1


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[_FakeSceneItem] = []
        self.released = 0
        self._source = types.SimpleNamespace(_ptr=f"scene:{name}")

    def add(self, source) -> _FakeSceneItem:
        item = _FakeSceneItem(source)
        self.items.append(item)
        return item

    def as_source(self):
        return self._source

    def release(self) -> None:
        self.released += 1


class _FakeSceneFactory:
    def __init__(self, registry: list[_FakeScene]) -> None:
        self._registry = registry

    def create(self, name: str) -> _FakeScene:
        scene = _FakeScene(name)
        self._registry.append(scene)
        return scene


class _FakeOb:
    def __init__(self, registry: list[_FakeSource]) -> None:
        self.Source = _FakeSourceFactory(registry)
        self.OBSData = dict  # ObsMediaController wraps speed settings in OBSData(dict)
        self.VideoFormat = _VideoFormat
        self.BoundsType = _BoundsType
        self.Alignment = _Alignment
        self.scenes: list[_FakeScene] = []
        self.Scene = _FakeSceneFactory(self.scenes)
        self.raw_callbacks: list = []
        self.removed_callbacks: list = []

    def add_raw_video_callback(self, fn, format, width, height):  # noqa: A002 - mirrors API
        handle = (fn, format, width, height)
        self.raw_callbacks.append(handle)
        return handle

    def remove_raw_video_callback(self, handle) -> None:
        self.removed_callbacks.append(handle)


class _FakeVideo:
    width = 1920
    height = 1080
    fps = 30


class _FakeRuntime:
    def __init__(self, registry: list[_FakeSource]) -> None:
        self.ob = _FakeOb(registry)
        self.video = _FakeVideo()
        self.started = True
        self.ensure_started_calls = 0
        self.channels: list[int] = []
        self.channel_sources: dict[int, object] = {}
        self.monitoring: list[tuple[object, int]] = []
        self.released_channels: list[int] = []

    def ensure_started(self, **_kwargs) -> None:
        self.ensure_started_calls += 1

    def acquire_channel(self) -> int:
        channel = len(self.channels)
        self.channels.append(channel)
        return channel

    def set_channel_source(self, channel: int, source) -> None:
        self.channel_sources[channel] = source

    def set_source_monitoring(self, source, monitoring_type: int) -> None:
        self.monitoring.append((source, monitoring_type))

    def release_channel(self, channel: int) -> None:
        self.released_channels.append(channel)
        self.channel_sources.pop(channel, None)


class _MediaSettings:
    def __init__(self, auto_download: bool) -> None:
        self._auto_download = auto_download

    def auto_download_on_play(self) -> bool:
        return self._auto_download


class _Downloader(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, bool]] = []
        self.cancel_count = 0
        self._cached: str | None = None

    def set_cached(self, path: str | None) -> None:
        self._cached = path

    def get_cached_path(self, _url: str):
        return self._cached

    def start(self, url: str, persist: bool = True) -> None:
        self.started.append((url, persist))

    def cancel(self) -> None:
        self.cancel_count += 1


class _FakeProbe:
    def __init__(self) -> None:
        self.probed: list[str] = []
        self.stopped = 0

    def probe(self, source: str) -> None:
        self.probed.append(source)

    def stop(self) -> None:
        self.stopped += 1


class _FakeProgram:
    """Stand-in for the projection program singleton (no real libobs)."""

    def __init__(self) -> None:
        self.ensured = 0
        self.media_sources: list = []
        self.detach_calls = 0
        self.current_key: str | None = None

    def ensure(self) -> None:
        self.ensured += 1

    def show_media(self, source) -> None:
        self.media_sources.append(source)
        self.current_key = "media"

    def detach_media(self) -> None:
        self.detach_calls += 1


def _make(monkeypatch, *, auto_download: bool = False, tmp_path=None, projection: bool = True):
    import solin.core.media.obs_program as obs_program

    registry: list[_FakeSource] = []
    runtime = _FakeRuntime(registry)
    monkeypatch.setattr(obs_playback, "obs_runtime", lambda: runtime)
    program = _FakeProgram()
    # The engine imports projection_program() lazily inside _create_source, so
    # patching the attribute on the module keeps libobs out.
    monkeypatch.setattr(obs_program, "projection_program", lambda: program)
    downloader = _Downloader()
    cache_manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: _Downloader(),
    )
    controller = ObsMediaController(
        _MediaSettings(auto_download),
        cache_manager,
        downloader_factory=lambda _parent: downloader,
        projection=projection,
    )
    # Replace the real Qt metadata probe with a recorder (no real file decode).
    controller._metadata_probe = _FakeProbe()
    controller._fake_program = program
    return controller, runtime, downloader, registry


# ── start_playback source resolution ─────────────────────────────────────────


def test_local_file_routes_source_through_program_with_monitoring(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    source_changed: list[bool] = []
    controller.playback_source_changed.connect(source_changed.append)

    controller.start_playback(MediaPlaybackRequest(str(local)))

    assert runtime.ensure_started_calls == 1
    assert len(registry) == 1
    source = registry[0]
    assert source.kind == "ffmpeg_source"
    assert source.settings == {"is_local_file": True, "local_file": str(local)}
    # The foreground engine hands the source to the projection program, which
    # wraps it in a canvas-filling scene and crossfades — no channel/scene here.
    assert controller._fake_program.media_sources == [source]
    assert runtime.channel_sources == {}
    assert runtime.ob.scenes == []
    # monitoring stays on the ffmpeg source (that's where the audio is)
    assert runtime.monitoring == [(source, MONITORING_MONITOR_ONLY)]
    # autoplay → play_pause(False)
    assert source.play_pause_calls == [False]
    assert controller.current_url == str(local)
    assert controller.local_path == str(local)
    assert controller.stream_persist is False
    assert source_changed == [True]
    controller.shutdown()


def test_remote_cached_plays_local_file(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"cached-bytes")
    downloader.set_cached(str(cached))
    buffer_events: list[tuple[int, int]] = []
    controller.buffer_progress.connect(lambda d, t: buffer_events.append((d, t)))

    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    source = registry[-1]
    assert source.settings == {"is_local_file": True, "local_file": str(cached)}
    assert controller.stream_persist is True
    assert controller.local_path == str(cached)
    # buffer shows fully cached
    size = cached.stat().st_size
    assert buffer_events[-1] == (size, size)
    assert downloader.started == []  # already cached, no new download
    controller.shutdown()


def test_remote_uncached_streams_url_and_starts_download(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    source_changed: list[bool] = []
    controller.playback_source_changed.connect(source_changed.append)

    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    source = registry[-1]
    assert source.settings == {"is_local_file": False, "input": "https://cdn.example/song.mp3"}
    assert downloader.started == [("https://cdn.example/song.mp3", True)]
    assert controller.stream_persist is True
    assert controller.local_path is None
    assert source_changed == [False]  # streaming, not offline
    controller.shutdown()


def test_temporary_cache_policy_streams_without_persist(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )

    controller.start_playback(
        MediaPlaybackRequest(
            "https://cdn.example/song.mp3",
            cache_policy=PlaybackCachePolicy.TEMPORARY,
        )
    )

    assert downloader.started == [("https://cdn.example/song.mp3", False)]
    assert controller.stream_persist is False
    controller.shutdown()


def test_autoplay_false_starts_paused(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")

    controller.start_playback(MediaPlaybackRequest(str(local), autoplay=False))

    assert registry[-1].play_pause_calls == [True]  # paused
    assert controller.is_playing is False
    controller.shutdown()


# ── transport ────────────────────────────────────────────────────────────────


def test_transport_play_pause_seek_replay_stop(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.play_pause_calls.clear()

    controller.pause()
    controller.play()
    assert source.play_pause_calls == [True, False]

    controller.seek(500)
    assert source.media_time == 500

    controller.replay()
    assert source.restarted == 1

    controller.stop()
    # The projection engine hands the source to the program for a fade-out
    # disposal rather than stopping/removing it here — releasing it directly
    # would obs_source_remove it mid-fade and cut the video to black.
    assert controller._fake_program.detach_calls == 1
    assert source.stopped == 0
    assert controller.current_url == ""


def test_seek_is_relative_to_trim_start(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    # simulate a resolved trim range starting at 10s
    controller._playback_range = MediaTrim(
        start_trim_ticks=10_000 * 10_000,
        end_trim_ticks=20_000 * 10_000,
    ).resolve(120_000)

    controller.seek(500)

    assert source.media_time == 10_000 + 500
    controller.shutdown()


# ── poll → signals ───────────────────────────────────────────────────────────


def test_poll_emits_duration_position_and_state(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    durations: list[int] = []
    source_durations: list[int] = []
    positions: list[int] = []
    states: list[object] = []
    controller.duration_changed.connect(durations.append)
    controller.source_duration_changed.connect(source_durations.append)
    controller.position_changed.connect(positions.append)
    controller.state_changed.connect(states.append)

    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_duration = 3000
    source.media_time = 1200
    source.media_state = 1  # playing

    controller._on_poll()

    assert source_durations == [3000]
    assert durations == [3000]
    assert positions[-1] == 1200
    assert states and states[-1] == PlaybackState.PlayingState
    # duration is emitted once, not on every poll
    controller._on_poll()
    assert source_durations == [3000]
    controller.shutdown()


def test_poll_emits_media_ended_once_on_ended_state(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_duration = 3000
    source.media_state = 6  # OBS "ended"

    controller._on_poll()
    controller._on_poll()

    assert ended == [True]
    controller.shutdown()


def test_poll_reports_error_state(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_state = 7  # OBS "error"

    controller._on_poll()

    assert errors and errors[-1] == "Playback failed."
    controller.shutdown()


# ── trim gating ──────────────────────────────────────────────────────────────


def test_trim_range_seeks_to_start_and_reports_relative_timeline(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    durations: list[int] = []
    ranges: list[tuple[int, int]] = []
    positions: list[int] = []
    controller.duration_changed.connect(durations.append)
    controller.playback_range_changed.connect(lambda d, s: ranges.append((d, s)))
    controller.position_changed.connect(positions.append)

    controller.start_playback(
        MediaPlaybackRequest(
            str(local),
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )
    source = registry[-1]
    source.media_duration = 120_000
    source.media_time = 10_000  # at trim start

    controller._on_poll()

    # effective (trimmed) duration is 90s; range resolved and seeked to start
    assert durations == [90_000]
    assert ranges == [(90_000, 10_000)]
    assert source.media_time == 10_000
    assert positions[-1] == 0  # relative position at the trim start
    controller.shutdown()


def test_trim_clip_is_muted_until_the_start_seek_lands(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")

    controller.start_playback(
        MediaPlaybackRequest(
            str(local),
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )
    source = registry[-1]
    # Muted at create — before the seek — so no wrong-position audio blips out
    # during the ffmpeg_source's brief pre-roll from position 0.
    assert source.muted is True
    assert source.volume == 0.0

    source.media_duration = 120_000
    source.media_time = 10_000
    controller._on_poll()  # resolves the trim + seeks to start_ms

    # Once the seek lands the user's volume/mute is restored.
    assert source.muted is False
    assert source.volume == 1.0
    controller.shutdown()


def test_untrimmed_clip_is_not_muted_at_start(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")

    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]

    assert source.muted is False
    assert source.volume == 1.0
    controller.shutdown()


def test_trim_end_pauses_and_emits_media_ended_once(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    ended: list[bool] = []
    positions: list[int] = []
    controller.media_ended.connect(lambda: ended.append(True))
    controller.position_changed.connect(positions.append)
    controller.start_playback(
        MediaPlaybackRequest(
            str(local),
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )
    source = registry[-1]
    source.media_duration = 120_000
    source.media_time = 10_000
    controller._on_poll()  # resolves range (end at 100_000)
    source.play_pause_calls.clear()

    # advance past the trim end
    source.media_time = 100_500
    controller._on_poll()
    controller._on_poll()

    assert source.play_pause_calls[:1] == [True]  # paused at end
    assert positions[-1] == 90_000  # clamped to trimmed duration
    assert ended == [True]
    controller.shutdown()


# ── audio_output shim ────────────────────────────────────────────────────────


def test_audio_output_shim_drives_source_volume_and_mute(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]

    controller.set_volume(0.5)
    assert source.volume == 0.5
    assert controller.volume == 0.5

    controller.audio_output.setMuted(True)
    assert source.muted is True
    assert source.volume == 0.0  # muted forces source volume to 0

    controller.audio_output.setMuted(False)
    assert source.muted is False
    assert source.volume == 0.5  # restores remembered volume
    controller.shutdown()


def test_volume_set_before_playback_applies_to_new_source(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")

    controller.set_volume(0.3)  # no source yet
    controller.start_playback(MediaPlaybackRequest(str(local)))

    assert registry[-1].volume == 0.3
    controller.shutdown()


# ── download switch-to-local ─────────────────────────────────────────────────


def test_download_finished_switches_stream_to_local_file(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    source_changed: list[bool] = []
    controller.playback_source_changed.connect(source_changed.append)
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))
    streaming_source = registry[-1]
    streaming_source.media_time = 4200  # mid-playback position

    local = tmp_path / "downloaded.mp3"
    local.write_bytes(b"complete")
    downloader.finished.emit(str(local))

    # the streamed source is handed to the program for a fade-out (not stopped
    # here) and a local-file source takes over via the program
    assert controller._fake_program.detach_calls >= 1
    new_source = registry[-1]
    assert new_source is not streaming_source
    assert new_source.settings == {"is_local_file": True, "local_file": str(local)}
    assert controller._fake_program.media_sources[-1] is new_source
    assert new_source.media_time == 4200  # position preserved
    assert controller.local_path == str(local)
    assert source_changed[-1] is True  # now offline/persistent
    controller.shutdown()


def test_download_error_reports_failure_for_remote(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    failures: list[tuple[str, str, bool]] = []
    buffer_events: list[tuple[int, int]] = []
    controller.playback_download_failed.connect(
        lambda url, msg, persist: failures.append((url, msg, persist))
    )
    controller.buffer_progress.connect(lambda d, t: buffer_events.append((d, t)))
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    downloader.error.emit("No space left on device")

    assert failures == [
        ("https://cdn.example/song.mp3", "No space left on device", True)
    ]
    assert buffer_events[-1] == (0, 0)
    controller.shutdown()


# ── runtime unavailable ──────────────────────────────────────────────────────


def test_start_playback_reports_error_when_runtime_unavailable(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)

    def _boom(**_kwargs):
        from solin.core.media.obs_runtime import ObsRuntimeError

        raise ObsRuntimeError("no libobs here")

    monkeypatch.setattr(runtime, "ensure_started", _boom)

    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    assert errors and "no libobs here" in errors[-1]
    assert registry == []  # no source created


# ── playback rate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rate,expected_percent",
    [(1.0, 100), (2.0, 200), (0.5, 50), (0.1, 10), (0.0, 1), (5.0, 200)],
)
def test_set_playback_rate_uses_speed_percent_and_clamps(
    monkeypatch, tmp_path, rate, expected_percent
):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]

    controller.set_playback_rate(rate)

    # ffmpeg_source's key is "speed_percent" (not "speed"), clamped to 1..200
    assert source.updates == [{"speed_percent": expected_percent}]
    controller.shutdown()


def test_set_playback_rate_without_source_is_noop(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    controller.set_playback_rate(1.5)  # no source yet — must not raise
    assert registry == []


# ── state mapping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "obs_state,expected",
    [
        (0, PlaybackState.StoppedState),  # none
        (1, PlaybackState.PlayingState),  # playing
        (2, PlaybackState.StoppedState),  # opening
        (3, PlaybackState.PlayingState),  # buffering → playing
        (4, PlaybackState.PausedState),  # paused
        (5, PlaybackState.StoppedState),  # stopped
        (6, PlaybackState.StoppedState),  # ended
        (7, PlaybackState.StoppedState),  # error
    ],
)
def test_map_state_covers_every_obs_media_state(obs_state, expected):
    assert ObsMediaController._map_state(obs_state) == expected


# ── shutdown ─────────────────────────────────────────────────────────────────


def test_shutdown_releases_channel(monkeypatch, tmp_path):
    # Only the audio-only (background) engine uses a private channel.
    controller, runtime, downloader, registry = _make(
        monkeypatch, tmp_path=tmp_path, projection=False
    )
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))

    controller.shutdown()

    assert runtime.released_channels == [0]
    assert controller._channel is None
    # a subsequent playback re-acquires a channel
    controller.start_playback(MediaPlaybackRequest(str(local)))
    assert controller._channel == 1
    controller.shutdown()


# ── trimmed replay ───────────────────────────────────────────────────────────


def test_replay_trimmed_seeks_to_start_and_rearms_media_ended(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    controller.start_playback(
        MediaPlaybackRequest(
            str(local),
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )
    source = registry[-1]
    source.media_duration = 120_000
    source.media_time = 10_000
    controller._on_poll()  # resolve range (end at 100_000)
    source.media_time = 100_500
    controller._on_poll()  # hit end → media_ended
    assert ended == [True]

    controller.replay()

    assert source.restarted == 1
    assert source.media_time == 10_000  # re-seeked to trim start
    # media_ended can fire again after replay
    source.media_time = 100_500
    controller._on_poll()
    assert ended == [True, True]
    controller.shutdown()


# ── toggle ───────────────────────────────────────────────────────────────────


def test_toggle_play_pause_follows_last_state(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))  # starts Playing
    source = registry[-1]
    source.play_pause_calls.clear()

    controller.toggle_play_pause()  # Playing → pause
    assert source.play_pause_calls == [True]

    source.media_state = 4  # paused
    controller._on_poll()  # updates last_state to Paused
    controller.toggle_play_pause()  # Paused → play
    assert source.play_pause_calls == [True, False]
    controller.shutdown()


# ── buffer progress + unknown duration ───────────────────────────────────────


def test_download_progress_forwards_to_buffer(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    buffer_events: list[tuple[int, int]] = []
    controller.buffer_progress.connect(lambda d, t: buffer_events.append((d, t)))
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    downloader.progress.emit(512, 2048)

    assert buffer_events[-1] == (512, 2048)
    controller.shutdown()


def test_unknown_duration_does_not_emit_duration(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    durations: list[int] = []
    controller.duration_changed.connect(durations.append)
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_duration = -1  # real ffmpeg_source reports -1 when unknown

    controller._on_poll()

    assert durations == []  # nothing emitted while duration is unknown
    controller.shutdown()


# ── preview-frame output (gated raw callback → frame_ready) ───────────────────


def _start_local(controller, tmp_path):
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))


def test_frame_output_off_by_default(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)

    assert runtime.ob.raw_callbacks == []  # no raw callback while nobody watches
    controller.shutdown()


def test_enable_frame_output_registers_callback_and_emits_frame(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)
    frames: list[object] = []
    controller.frame_ready.connect(frames.append)

    controller.set_frame_output_enabled(True)

    # a raw callback was registered at the canvas size, in BGRA
    assert len(runtime.ob.raw_callbacks) == 1
    fn, fmt, width, height = runtime.ob.raw_callbacks[0]
    assert (width, height) == (runtime.video.width, runtime.video.height)
    assert fmt == runtime.ob.VideoFormat.BGRA

    # libobs delivers a frame (graphics thread) → buffered → timer emits it
    w, h, stride = 4, 2, 16
    fn([b"\x00" * (stride * h)], [stride], w, h, fmt, 0)
    controller._emit_buffered_frame()

    assert len(frames) == 1
    frame = frames[0]
    # The payload is a plain QImage: every consumer immediately called toImage()
    # on the QVideoFrame this used to be wrapped in, so the wrap was a round-trip.
    assert isinstance(frame, QImage)
    assert not frame.isNull()
    assert (frame.width(), frame.height()) == (w, h)
    controller.shutdown()


def test_emit_without_buffered_frame_is_noop(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)
    controller.set_frame_output_enabled(True)
    frames: list[object] = []
    controller.frame_ready.connect(frames.append)

    controller._emit_buffered_frame()  # nothing buffered yet

    assert frames == []
    controller.shutdown()


def test_frame_output_coalesces_to_latest_frame(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)
    controller.set_frame_output_enabled(True)
    frames: list[object] = []
    controller.frame_ready.connect(frames.append)
    fn = runtime.ob.raw_callbacks[0][0]

    fn([b"\x00" * 32], [16], 4, 2, 5, 0)  # dropped
    fn([b"\x00" * 96], [16], 4, 4, 5, 0)  # latest wins
    controller._emit_buffered_frame()

    assert len(frames) == 1
    assert (frames[0].width(), frames[0].height()) == (4, 4)
    controller.shutdown()


def test_disable_frame_output_removes_callback(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)
    controller.set_frame_output_enabled(True)
    handle = runtime.ob.raw_callbacks[0]

    controller.set_frame_output_enabled(False)

    assert runtime.ob.removed_callbacks == [handle]
    assert controller._frame_output_active is False
    controller.shutdown()


def test_frame_output_deferred_until_source_exists(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)

    controller.set_frame_output_enabled(True)  # no source yet
    assert runtime.ob.raw_callbacks == []  # nothing registered

    _start_local(controller, tmp_path)  # source arrives → output starts
    assert len(runtime.ob.raw_callbacks) == 1
    controller.shutdown()


def test_frame_output_stops_when_playback_stops(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    _start_local(controller, tmp_path)
    controller.set_frame_output_enabled(True)
    handle = runtime.ob.raw_callbacks[0]

    controller.stop()

    assert runtime.ob.removed_callbacks == [handle]
    assert controller._frame_output_active is False


# ── metadata probe (title + cover art) ───────────────────────────────────────


def test_start_playback_probes_metadata_of_played_source(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp3"
    local.write_bytes(b"x")

    controller.start_playback(MediaPlaybackRequest(str(local)))

    assert controller._metadata_probe.probed == [str(local)]
    controller.stop()
    assert controller._metadata_probe.stopped == 1


def test_extract_cover_bytes_across_tag_formats():
    """The cover-byte extractor handles FLAC/OGG pictures, ID3 APIC and MP4 covr
    (pure byte logic — no Qt, so it never touches the cumulative-teardown path)."""
    from solin.core.media.obs_playback import _extract_cover_bytes

    class _Pic:
        def __init__(self, data):
            self.data = data

    flac = types.SimpleNamespace(pictures=[_Pic(b"flac-bytes")], tags=None)
    assert _extract_cover_bytes(flac) == b"flac-bytes"

    class _APIC:
        def __init__(self, data):
            self.data = data

    class _Id3Tags:
        def getall(self, key):
            return [_APIC(b"id3-bytes")] if key == "APIC" else []

        def get(self, key, default=None):
            return default

    mp3 = types.SimpleNamespace(pictures=None, tags=_Id3Tags())
    assert _extract_cover_bytes(mp3) == b"id3-bytes"

    class _Mp4Tags:  # no getall() → APIC path skipped
        def get(self, key, default=None):
            return [b"mp4-bytes"] if key == "covr" else default

    mp4 = types.SimpleNamespace(pictures=None, tags=_Mp4Tags())
    assert _extract_cover_bytes(mp4) == b"mp4-bytes"

    bare = types.SimpleNamespace(pictures=None, tags=None)
    assert _extract_cover_bytes(bare) is None
    assert _extract_cover_bytes(None) is None


def test_read_media_tag_bytes_reads_title_and_id3_cover(monkeypatch):
    """The mutagen-backed reader recovers the title + cover BYTES, no Qt."""
    from solin.core.media import obs_playback as mod

    class _APIC:
        def __init__(self, data):
            self.data = data

    class _Id3Tags:
        def getall(self, key):
            return [_APIC(b"cover-bytes")] if key == "APIC" else []

        def get(self, key, default=None):
            return default

    fake = types.ModuleType("mutagen")

    def _file(path, easy=False):
        if easy:
            return {"title": ["  Kingdom Song 5  "]}
        return types.SimpleNamespace(pictures=None, tags=_Id3Tags())

    fake.File = _file
    monkeypatch.setitem(sys.modules, "mutagen", fake)

    title, cover_bytes = mod._read_media_tag_bytes("song.mp3")

    assert title == "Kingdom Song 5"  # trimmed
    assert cover_bytes == b"cover-bytes"


def _drain_probe():
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    QThreadPool.globalInstance().waitForDone(3000)  # finish the worker read
    QApplication.processEvents()  # deliver the queued cross-thread result


def test_metadata_probe_delivers_title_off_thread(monkeypatch, tmp_path):
    from solin.core.media import obs_playback as mod
    from solin.core.media.obs_playback import _MetadataProbe

    local = tmp_path / "song.mp3"
    local.write_bytes(b"x")
    # The reader runs on a worker thread and returns title + cover BYTES; the
    # result is marshalled back to the GUI thread (cover bytes → QPixmap there).
    monkeypatch.setattr(
        mod, "_read_media_tag_bytes", lambda _p: ("Kingdom Song 5", None)
    )

    probe = _MetadataProbe()
    titles: list[str] = []
    covers: list[object] = []
    probe.title_found.connect(titles.append)
    probe.cover_found.connect(covers.append)

    probe.probe(str(local))
    _drain_probe()

    assert titles == ["Kingdom Song 5"]  # delivered after the off-thread read
    assert covers == [None]
    assert probe._last_title == "Kingdom Song 5"  # surfaced via the .player shim


def test_metadata_probe_discards_superseded_result(monkeypatch, tmp_path):
    from solin.core.media import obs_playback as mod
    from solin.core.media.obs_playback import _MetadataProbe

    local = tmp_path / "song.mp3"
    local.write_bytes(b"x")
    monkeypatch.setattr(
        mod, "_read_media_tag_bytes", lambda _p: ("Stale Title", None)
    )

    probe = _MetadataProbe()
    titles: list[str] = []
    probe.title_found.connect(titles.append)

    probe.probe(str(local))
    probe.stop()  # supersede the in-flight read before it is delivered
    _drain_probe()

    assert titles == []  # the stale worker result is discarded by the token


def test_metadata_probe_clears_cover_for_remote_source():
    from solin.core.media.obs_playback import _MetadataProbe

    probe = _MetadataProbe()
    covers: list[object] = []
    probe.cover_found.connect(covers.append)

    # mutagen can't read a remote URL — clear any stale cover; re-probed once cached.
    probe.probe("https://cdn.example/song.mp3")

    assert covers == [None]


# ── QMediaPlayer-compatible .player shim ──────────────────────────────────────


def test_player_shim_reflects_state(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    # callers reach into media_controller.player.playbackState()
    assert controller.player.playbackState() == PlaybackState.StoppedState

    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    controller.start_playback(MediaPlaybackRequest(str(local)))

    assert controller.player.playbackState() == PlaybackState.PlayingState
    # metaData() is gone: title/cover reach the UI through title_from_metadata
    # and cover_art_changed, so the shim no longer needs QtMultimedia at all.
    assert not hasattr(controller.player, "metaData")
    controller.shutdown()


# ── parity hardening: handoff, speed, seek re-arm, metadata, prefetch ─────────


def test_trimmed_remote_clip_not_remuted_after_cache_handoff(monkeypatch, tmp_path):
    """A trimmed remote clip must not go permanently silent when the background
    download completes and the source is rebuilt: the range is already resolved,
    so the pre-roll mute must NOT be re-applied (it would never be lifted)."""
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    controller.start_playback(
        MediaPlaybackRequest(
            "https://cdn.example/clip.mp4",
            trim=MediaTrim(
                start_trim_ticks=10_000 * 10_000,
                end_trim_ticks=20_000 * 10_000,
                base_duration_ticks=120_000 * 10_000,
            ),
        )
    )
    streaming = registry[-1]
    assert streaming.muted is True  # pre-roll mute while streaming
    streaming.media_duration = 120_000
    streaming.media_time = 10_000
    controller._on_poll()  # resolves the trim range → lifts the pre-roll mute
    assert streaming.muted is False

    local = tmp_path / "clip.mp4"
    local.write_bytes(b"complete")
    streaming.media_time = 45_000  # mid-playback
    downloader.finished.emit(str(local))

    new_source = registry[-1]
    assert new_source is not streaming
    assert controller._trim_muted is False
    assert new_source.muted is False  # NOT re-muted → audible
    assert new_source.volume == 1.0
    assert new_source.media_time == 45_000  # position preserved
    controller.shutdown()


def test_playback_speed_survives_stream_to_local_handoff(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))
    streaming = registry[-1]
    controller.set_playback_rate(1.5)
    assert streaming.updates == [{"speed_percent": 150}]

    local = tmp_path / "song.mp3"
    local.write_bytes(b"complete")
    downloader.finished.emit(str(local))

    new_source = registry[-1]
    assert new_source is not streaming
    # The fresh local source is created already at 150% — not silently 100%.
    assert new_source.settings.get("speed_percent") == 150
    controller.shutdown()


def test_seek_rearms_media_ended(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_duration = 3000
    source.media_state = 6  # ended
    controller._on_poll()
    assert ended == [True]

    controller.seek(0)  # scrub back
    source.media_state = 6
    controller._on_poll()
    assert ended == [True, True]  # auto-advance re-arms
    controller.shutdown()


def test_download_finished_reprobes_metadata_for_remote_cover(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))
    controller._metadata_probe.probed.clear()  # drop the initial remote-URL probe

    local = tmp_path / "song.mp3"
    local.write_bytes(b"complete")
    downloader.finished.emit(str(local))

    # The now-local file is re-probed so remote cover/title finally surface.
    assert controller._metadata_probe.probed == [str(local)]
    controller.shutdown()


class _SpyCache:
    """Records the cache-manager calls the engine makes. is_remote stays on the
    real class (the engine calls MediaCacheManager.is_remote directly)."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.notified: list[str] = []

    def cancel_prefetch(self, url: str) -> None:
        self.cancelled.append(url)

    def notify_cached(self, url: str) -> None:
        self.notified.append(url)


def test_remote_playback_cancels_prefetch(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    spy = _SpyCache()
    controller._cache_manager = spy

    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))

    assert spy.cancelled == ["https://cdn.example/song.mp3"]
    controller.shutdown()


def test_local_playback_does_not_cancel_prefetch(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    spy = _SpyCache()
    controller._cache_manager = spy
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")

    controller.start_playback(MediaPlaybackRequest(str(local)))

    assert spy.cancelled == []  # local sources have no prefetch to cancel
    controller.shutdown()


def test_obs_controller_has_no_video_sink(monkeypatch, tmp_path):
    """The playlist live-thumb guard relies on this: the libobs engine renders
    via an OBS Display, not a QVideoSink."""
    controller, _runtime, _downloader, _registry = _make(monkeypatch, tmp_path=tmp_path)
    assert getattr(controller, "video_sink", None) is None
    controller.shutdown()


# ── deferred local switch (Song Announcement Mode) ───────────────────────────


def test_deferred_switch_holds_swap_until_gate_clears(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    source_changed: list[bool] = []
    controller.playback_source_changed.connect(source_changed.append)
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))
    streaming = registry[-1]
    streaming.media_time = 8000

    controller.set_local_switch_deferred(True)
    local = tmp_path / "song.mp3"
    local.write_bytes(b"complete")
    downloader.finished.emit(str(local))

    # Still on the streamed source — the disruptive swap is held.
    assert registry[-1] is streaming
    assert controller._fake_program.media_sources[-1] is streaming
    assert source_changed[-1] is True  # offline badge already shown

    # Gate clears → the pending swap is applied from the current position.
    controller.set_local_switch_deferred(False)
    new_source = registry[-1]
    assert new_source is not streaming
    assert new_source.settings["local_file"] == str(local)
    assert new_source.media_time == 8000
    controller.shutdown()


# ── live-stream reconnect ────────────────────────────────────────────────────


def _start_remote_playing(controller, registry, *, duration=120_000, position=30_000):
    controller.start_playback(MediaPlaybackRequest("https://cdn.example/song.mp3"))
    source = registry[-1]
    source.media_duration = duration
    source.media_time = position
    source.media_state = 1  # playing
    controller._on_poll()  # marks remote-started + records last position
    return source


def test_remote_drop_before_end_schedules_reconnect_not_media_ended(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    ended: list[bool] = []
    interrupted: list[tuple[str, str]] = []
    recovery: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    controller.playback_interrupted.connect(lambda u, m: interrupted.append((u, m)))
    controller.playback_recovery_changed.connect(recovery.append)

    source = _start_remote_playing(controller, registry)
    source.media_state = 6  # ENDED well before the known duration → a drop
    controller._on_poll()

    assert ended == []  # NOT treated as a genuine end
    assert controller.is_recovering is True
    assert recovery == [True]
    assert interrupted and interrupted[0][0] == "https://cdn.example/song.mp3"
    assert controller._reconnect_timer.isActive()
    controller.shutdown()


def test_reconnect_reopens_stream_at_saved_position(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    source = _start_remote_playing(controller, registry, position=42_000)
    source.media_state = 6
    controller._on_poll()  # schedule reconnect
    count = len(registry)

    controller._do_reconnect()  # what the timer would call

    new_source = registry[-1]
    assert len(registry) == count + 1
    assert new_source is not source
    assert new_source.settings == {
        "is_local_file": False,
        "input": "https://cdn.example/song.mp3",
    }
    assert new_source.media_time == 42_000  # resumes where it dropped
    controller.shutdown()


def test_recovery_resets_when_stream_returns_healthy(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    recovery: list[bool] = []
    controller.playback_recovery_changed.connect(recovery.append)
    source = _start_remote_playing(controller, registry)
    source.media_state = 6
    controller._on_poll()  # enter recovery
    controller._do_reconnect()

    healed = registry[-1]
    healed.media_state = 1  # playing again
    healed.media_duration = 120_000
    healed.media_time = 30_000
    controller._on_poll()  # detect healthy → reset

    assert controller.is_recovering is False
    assert recovery == [True, False]
    controller.shutdown()


def test_remote_error_midplayback_reconnects(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)
    source = _start_remote_playing(controller, registry)
    source.media_state = 7  # error mid-playback

    controller._on_poll()

    assert errors == []  # not a hard failure — recover instead
    assert controller.is_recovering is True
    controller.shutdown()


def test_remote_end_at_full_duration_is_genuine(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    ended: list[bool] = []
    controller.media_ended.connect(lambda: ended.append(True))
    source = _start_remote_playing(controller, registry, position=119_900)
    source.media_state = 6  # reached the end → a genuine end, not a drop

    controller._on_poll()

    assert ended == [True]
    assert controller.is_recovering is False
    controller.shutdown()


def test_download_finishing_during_recovery_cancels_reconnect(monkeypatch, tmp_path):
    """If the cache download completes while a dropped stream is recovering, the
    switch to the local file must cancel recovery so a queued reconnect can't
    later re-stream over the finished file."""
    controller, runtime, downloader, registry = _make(
        monkeypatch, auto_download=True, tmp_path=tmp_path
    )
    recovery: list[bool] = []
    controller.playback_recovery_changed.connect(recovery.append)
    source = _start_remote_playing(controller, registry)
    source.media_state = 6
    controller._on_poll()  # enter recovery, reconnect timer armed
    assert controller.is_recovering is True
    assert controller._reconnect_timer.isActive()

    local = tmp_path / "song.mp3"
    local.write_bytes(b"complete")
    downloader.finished.emit(str(local))  # download beats the reconnect timer

    assert controller.is_recovering is False
    assert controller._reconnect_timer.isActive() is False
    assert recovery == [True, False]
    assert registry[-1].settings["local_file"] == str(local)
    controller.shutdown()


def test_local_error_still_fails_hard(monkeypatch, tmp_path):
    controller, runtime, downloader, registry = _make(monkeypatch, tmp_path=tmp_path)
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    errors: list[str] = []
    controller.error_occurred.connect(errors.append)
    controller.start_playback(MediaPlaybackRequest(str(local)))
    source = registry[-1]
    source.media_state = 7  # error on a local file → no reconnect

    controller._on_poll()

    assert errors == ["Playback failed."]
    assert controller.is_recovering is False
    controller.shutdown()
