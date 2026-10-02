"""Rate is split into atempo stages the filter will actually accept."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from solin.core.scenes import audio_tempo
from solin.core.scenes.audio_tempo import (
    atempo_chain,
    atempo_filter,
    is_unity_rate,
)


def _product(stages: list[float]) -> float:
    total = 1.0
    for stage in stages:
        total *= stage
    return total


@pytest.mark.parametrize("rate", [0.1, 0.25, 0.5, 0.75, 1.5, 2.0, 3.0, 4.0, 16.0])
def test_the_chain_multiplies_back_to_the_requested_rate(rate: float) -> None:
    assert math.isclose(_product(atempo_chain(rate)), rate, rel_tol=1e-6)


@pytest.mark.parametrize("rate", [0.1, 0.25, 0.5, 0.75, 1.5, 2.0, 3.0, 4.0, 16.0])
def test_every_stage_is_inside_the_filters_own_limits(rate: float) -> None:
    """atempo rejects anything outside 0.5-2.0, so a stage outside it never runs."""
    for stage in atempo_chain(rate):
        assert 0.5 <= stage <= 2.0, f"{rate} produced an out-of-range stage {stage}"


def test_normal_speed_needs_no_stretching() -> None:
    assert atempo_chain(1.0) == []
    assert is_unity_rate(1.0)
    assert is_unity_rate(1.0005), "an inaudible difference is not worth a re-decode"
    assert not is_unity_rate(1.05)


def test_the_stages_are_equal_rather_than_maximal() -> None:
    """Spread the artefacts across passes instead of one extreme pass."""
    stages = atempo_chain(4.0)

    assert len(stages) == 2
    assert stages[0] == stages[1]


def test_a_moderate_rate_uses_a_single_pass() -> None:
    for rate in (0.5, 0.75, 1.5, 2.0):
        assert len(atempo_chain(rate)) == 1, rate


def test_the_filter_string_is_a_comma_chain() -> None:
    assert atempo_filter(1.5) == "atempo=1.500000"
    assert atempo_filter(4.0) == "atempo=2.000000,atempo=2.000000"


def test_a_nonsense_rate_is_refused() -> None:
    for rate in (0.0, -1.0):
        with pytest.raises(ValueError):
            atempo_chain(rate)


# ── the companion's lifecycle, without spawning a real ffmpeg ────────────────


class _FakeSource:
    """Stands in for a libobs ffmpeg_source."""

    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = settings
        self.volume = 1.0
        self.paused: bool | None = None
        self.stopped = False
        self.released = False
        self._ptr = None  # no real pointer: activation is skipped

    def media_play_pause(self, paused: bool) -> None:
        self.paused = paused

    def media_stop(self) -> None:
        self.stopped = True

    def release(self) -> None:
        self.released = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.created: list[_FakeSource] = []
        self.monitored: list[tuple[_FakeSource, int]] = []
        self.ob = SimpleNamespace(Source=SimpleNamespace(create=self._create))

    def _create(self, kind: str, name: str, settings: dict) -> _FakeSource:
        source = _FakeSource(kind, name, settings)
        self.created.append(source)
        return source

    def set_source_monitoring(self, source: _FakeSource, mode: int) -> None:
        self.monitored.append((source, mode))


class _FakeProcess:
    def __init__(self, *args, **kwargs) -> None:
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode or 0


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    """Records ffmpeg invocations instead of running them."""
    calls: list[list[str]] = []

    def fake_popen(command, **kwargs):
        calls.append(list(command))
        return _FakeProcess()

    monkeypatch.setattr(audio_tempo.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(audio_tempo.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio_tempo, "_BIND_GRACE_S", 0.0)
    return calls


def test_it_serves_the_stretched_audio_to_a_source_that_reads_it(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)

    assert companion.start("/media/talk.mp4", position_ms=0, rate=1.5) is True

    served = spawned[0][-1]
    listened_on = served.rsplit(":", 1)[1].split("?", 1)[0]
    assert runtime.created[0].settings["input"].endswith(f":{listened_on}")


def test_the_stretch_resumes_from_where_playback_had_reached(spawned) -> None:
    companion = audio_tempo.TempoAudioCompanion(_FakeRuntime())
    companion.start("/media/talk.mp4", position_ms=90_500, rate=1.5)

    command = spawned[0]
    assert command[command.index("-ss") + 1] == "90.500"


def test_the_stretched_audio_is_monitored_so_it_reaches_the_speakers(spawned) -> None:
    from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY

    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5)

    assert runtime.monitored == [(runtime.created[0], MONITORING_MONITOR_ONLY)]
    assert runtime.created[0].paused is False


def test_it_opens_at_the_volume_the_player_is_set_to(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5, volume_percent=40)

    assert runtime.created[0].volume == pytest.approx(0.4)


def test_normal_speed_never_starts_a_second_decode(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)

    assert companion.start("/media/talk.mp4", position_ms=0, rate=1.0) is False
    assert spawned == []
    assert runtime.created == []
    assert companion.running is False


def test_without_ffmpeg_it_declines_instead_of_failing(spawned, monkeypatch) -> None:
    monkeypatch.setattr(audio_tempo.shutil, "which", lambda _name: None)
    companion = audio_tempo.TempoAudioCompanion(_FakeRuntime())

    assert companion.start("/media/talk.mp4", position_ms=0, rate=1.5) is False
    assert spawned == []


def test_a_stretch_that_dies_before_serving_leaves_nothing_running(
    spawned, monkeypatch
) -> None:
    """The caller then keeps libobs' own varispeed rather than losing audio."""
    dead = _FakeProcess()
    dead._returncode = 1
    monkeypatch.setattr(audio_tempo.subprocess, "Popen", lambda *a, **k: dead)
    monkeypatch.setattr(audio_tempo, "_BIND_GRACE_S", 0.05)
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)

    assert companion.start("/media/talk.mp4", position_ms=0, rate=1.5) is False
    assert runtime.created == []
    assert companion.running is False


def test_a_source_that_will_not_open_takes_the_process_down_with_it(
    spawned, monkeypatch
) -> None:
    runtime = _FakeRuntime()
    monkeypatch.setattr(
        runtime.ob.Source, "create", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
    )
    companion = audio_tempo.TempoAudioCompanion(runtime)

    assert companion.start("/media/talk.mp4", position_ms=0, rate=1.5) is False
    assert companion.running is False


def test_stopping_releases_the_source_and_ends_the_process(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5)
    process = companion._process

    companion.stop()

    source = runtime.created[0]
    assert (source.stopped, source.released) == (True, True)
    assert process.terminated is True
    assert companion.running is False


def test_stopping_twice_is_harmless(spawned) -> None:
    companion = audio_tempo.TempoAudioCompanion(_FakeRuntime())
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5)

    companion.stop()
    companion.stop()  # nothing left to stop


def test_restarting_replaces_the_previous_stretch(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5)

    companion.start("/media/talk.mp4", position_ms=12_000, rate=0.75)

    assert runtime.created[0].released is True  # the old one went away
    assert len(runtime.created) == 2
    assert len(spawned) == 2


def test_volume_and_pause_reach_the_running_source(spawned) -> None:
    runtime = _FakeRuntime()
    companion = audio_tempo.TempoAudioCompanion(runtime)
    companion.start("/media/talk.mp4", position_ms=0, rate=1.5)

    companion.set_volume(25)
    companion.set_paused(True)

    assert runtime.created[0].volume == pytest.approx(0.25)
    assert runtime.created[0].paused is True


def test_transport_calls_are_ignored_when_nothing_is_running() -> None:
    companion = audio_tempo.TempoAudioCompanion(_FakeRuntime())

    companion.set_volume(50)
    companion.set_paused(True)  # no source: must not raise
