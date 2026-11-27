import pytest

from solin.core.scenes.media_audio_activity import MediaAudioActivity, playback_silence_hold_ms


def test_sustained_silence_and_return_of_audio():
    now_ms = 0
    activity = MediaAudioActivity(800, monotonic_ns=lambda: now_ms * 1_000_000)

    def sample(ms: int) -> bool:
        nonlocal now_ms
        now_ms = ms
        return activity.sample(ms, playing=True, speed_percent=100)

    assert not sample(0)
    activity.observe_peak(-20.0)
    activity.observe_peak(float("-inf"))
    assert not sample(799)
    assert sample(800)

    activity.observe_peak(-57.0)  # within the hysteresis band
    assert sample(801)
    activity.observe_peak(-20.0)
    assert not sample(801)  # audio can return between transport position updates
    activity.observe_peak(-57.0)
    assert not sample(1500)  # absorbed as audio on the way down


def test_advancing_video_confirms_silence_but_network_stalls_and_seeks_reset_it():
    now_ms = 0
    activity = MediaAudioActivity(800, monotonic_ns=lambda: now_ms * 1_000_000)

    def sample(wall_ms: int, media_ms: int, *, playing: bool = True) -> bool:
        nonlocal now_ms
        now_ms = wall_ms
        return activity.sample(media_ms, playing=playing, speed_percent=200)

    assert not sample(0, 0)
    assert not sample(900, 300)  # decoder starved, only 150 ms advanced at 2x
    assert not sample(1800, 300, playing=False)
    assert not sample(2000, 300)
    assert not sample(2799, 1898)
    assert sample(2800, 1900)  # advancing video, no audio packets
    activity.reset()  # explicit seek discards the previous quiet interval
    assert not sample(2900, 6000)
    assert sample(3700, 7600)
    assert not sample(3750, 100)  # unexpected rewind also resets the interval


@pytest.mark.parametrize(("value", "hold_ms"), [(None, 800), ("0", 0), ("1200", 1200), ("bad", 800)])
def test_silence_hold_override(monkeypatch, value, hold_ms):
    flag = "SOLIN_ZOOM_PLAYBACK_SILENCE_MS"
    if value is None:
        monkeypatch.delenv(flag, raising=False)
    else:
        monkeypatch.setenv(flag, value)
    assert playback_silence_hold_ms() == hold_ms
