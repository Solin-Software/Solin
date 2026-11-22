"""Native dynamic playback-rate contracts, isolated from the pytest process."""

from __future__ import annotations

from array import array
from contextlib import ExitStack, contextmanager
import math
from pathlib import Path
from statistics import median
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time
from typing import NamedTuple
import wave

import pytest

from tests.fixtures.av_clip import (
    AV_FRAME_NUMBER_BITS,
    AV_FRAME_NUMBER_ROWS,
    AV_HIGH_TONE_HZ,
    AV_LOW_TONE_HZ,
    write_av_clip,
)
from tests.integration.test_native_media_startup import (
    _RED_BGRA,
    _capture_prepared_source,
    _native_media,
    _serve_clip,
    _wait_for,
)

pytestmark = pytest.mark.integration


def _poll(media):
    media.apply_startup_transport()
    return media.position_ms


def _playing(media, minimum=400):
    from solin.core.scenes.libobs_media_source import STATE_PLAYING

    return media.state == STATE_PLAYING and _poll(media) >= minimum


def _measure_progress(media, seconds=0.8):
    samples = []
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        position = _poll(media)
        samples.append((time.monotonic(), position))
        time.sleep(0.02)
    return samples


def _assert_rate(samples, rate):
    elapsed = samples[-1][0] - samples[0][0]
    progress = samples[-1][1] - samples[0][1]
    slope = progress / (elapsed * 1000)
    print(
        "rate observation",
        {"requested": rate, "slope": slope, "positions": [p for _, p in samples]},
        flush=True,
    )
    assert rate / 100 * 0.65 <= slope <= rate / 100 * 1.35, slope
    assert all(b >= a - 100 for (_, a), (_, b) in zip(samples, samples[1:], strict=False)), samples


def _measure_audio_progress(media, blocks, lock, sample_rate, speed, duration):
    with lock:
        blocks.clear()
        started = time.monotonic()
    samples = _measure_progress(media, duration)
    with lock:
        elapsed = time.monotonic() - started
        recorded = list(blocks)
    _assert_rate(samples, speed)
    pcm = array("f", b"".join(block[4] for block in recorded))
    # The native producer keeps running while the observer is descheduled.
    # Bound pacing against the same wall-clock window as the captured PCM.
    assert sample_rate * elapsed * 0.7 <= len(pcm) <= sample_rate * elapsed * 1.3, (
        speed, len(pcm), elapsed, duration,
    )
    return recorded, pcm


def _change_rate_and_measure(runtime, media, speed, duration):
    acknowledged = Event()
    ack_times = []

    def updated():
        ack_times.append(time.monotonic())
        acknowledged.set()

    disconnect = runtime.observe_source_updates(media.source, updated)
    try:
        before = _poll(media)
        requested_at = time.monotonic()
        media.set_speed(speed)
        assert acknowledged.wait(0.25), (speed, "native update was not acknowledged")
        latency = ack_times[0] - requested_at
        print(
            "rate update acknowledgment",
            {"speed": speed, "latency": latency},
            flush=True,
        )
        assert latency <= 0.25, latency
        # The native rate contract checks frame scheduling with a controlled clock.
        samples = _measure_progress(media, duration)
        assert min(position for _, position in samples) >= before - 100, (before, samples)
        _assert_rate(samples, speed)
    finally:
        disconnect()


def _transport(origin):
    from solin.core.scenes.libobs_media_source import STATE_PAUSED, STATE_PLAYING

    with _native_media() as (runtime, media, path), ExitStack() as stack:
        requests = []
        location = (
            stack.enter_context(_serve_clip(path, requests=requests))
            if origin == "http"
            else str(path)
        )
        stack.callback(media.close)
        assert media.open(location, volume_percent=0)
        assert _wait_for(lambda: _playing(media, 1200), timeout=8), media.position_ms
        # The native input has already opened and decoded; rate updates must not reopen it.
        count = len(requests)
        for speed, duration in ((150, 1.0), (200, 0.8), (25, 1.5)):
            _change_rate_and_measure(runtime, media, speed, duration)
            assert len(requests) == count, (count, requests)

        media.pause()
        assert _wait_for(lambda: media.state == STATE_PAUSED), media.state
        paused_at = _poll(media)
        for speed in (200, 25, 150, 50):
            media.set_speed(speed)
        time.sleep(0.4)
        assert media.state == STATE_PAUSED, media.state
        assert abs(_poll(media) - paused_at) <= 150, (paused_at, media.position_ms)
        assert len(requests) == count, (count, requests)
        media.play()
        assert _wait_for(lambda: media.state == STATE_PLAYING)
        time.sleep(0.35)
        _assert_rate(_measure_progress(media, 1.0), 50)
        assert len(requests) == count, (count, requests)


def _seek_and_restart():
    from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PLAYING

    with _native_media() as (_runtime, media, path):
        assert media.open(str(path), volume_percent=0)
        assert _wait_for(lambda: _playing(media), timeout=8)
        media.set_speed(150)
        time.sleep(0.3)
        for target in (5000, 2000):
            media.seek(target)
            assert _wait_for(lambda target=target: target - 300 <= _poll(media) <= target + 900), (
                target,
                media.position_ms,
            )
            time.sleep(0.3)
            _assert_rate(_measure_progress(media), 150)
        media.seek(9400)
        assert _wait_for(lambda: media.state == STATE_ENDED, timeout=5), (
            media.state,
            media.position_ms,
        )
        media.restart()
        assert _wait_for(
            lambda: media.state == STATE_PLAYING and 100 < _poll(media) < 2000, timeout=5
        ), (media.state, media.position_ms)
        time.sleep(0.3)
        _assert_rate(_measure_progress(media), 150)


def _write_tone(path, *, seconds=40):
    sample_rate = 48000
    period = b"".join(
        struct.pack("<h", round(16000 * math.sin(2 * math.pi * 1000 * i / sample_rate)))
        for i in range(48)
    )
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(period * (sample_rate // 48) * seconds)


def _write_rate_pattern(path):
    sample_rate = 48000

    def segment(frequency):
        period_samples = sample_rate // frequency
        period = b"".join(
            struct.pack("<h", round(16000 * math.sin(2 * math.pi * i / period_samples)))
            for i in range(period_samples)
        )
        return period * (sample_rate // 5 // period_samples)

    # Each alternating tone covers exactly 200 ms of the media timeline.
    pattern = segment(400) + segment(1000)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(pattern * 25)


def _audio_rate_observation(recorded, sample_rate, final_ack):
    intervals = [
        current[0] - previous[0] for previous, current in zip(recorded, recorded[1:], strict=False)
    ]
    assert intervals, "Not enough audio callbacks to measure recovery"
    p95_gap = sorted(intervals)[math.ceil(len(intervals) * 0.95) - 1]
    segments = []
    window = sample_rate // 50  # 20 ms of output audio
    frequencies = []
    output_anchor = recorded[0][0]
    pcm = array("f", b"".join(block[4] for block in recorded))
    for offset in range(0, len(pcm) - window + 1, window):
        part = pcm[offset : offset + window]
        crossings = sum(a <= 0 < b for a, b in zip(part, part[1:], strict=False))
        frequency = crossings * sample_rate / len(part)
        frequencies.append(frequency)
        if frequency < 250:
            continue
        segments.append((output_anchor + offset / sample_rate, frequency >= 700))
    runs = []
    for at, high in segments:
        if not runs or runs[-1][2] != high:
            runs.append([at, at, high])
        else:
            runs[-1][1] = at
    complete = [
        (round(start - final_ack, 3), round(end - start + 0.02, 3), high)
        for start, end, high in runs[1:-1]
    ]
    boundary = next(
        (
            first[0]
            for first, second in zip(complete, complete[1:], strict=False)
            if first[0] >= 0 and 0.09 <= first[1] <= 0.18 and 0.09 <= second[1] <= 0.18
        ),
        None,
    )
    print(
        "rapid audio timing",
        {
            "callback_p95_gap": p95_gap,
            "output_anchor": "first callback arrival; uncertainty at least one callback gap",
            "estimated_150_percent_after_ack": boundary,
            "tone_runs_since_ack": complete,
            "all_runs": [
                (round(start - final_ack, 3), round(end - start + 0.02, 3), high)
                for start, end, high in runs
            ],
            "frequency_range": (min(frequencies), max(frequencies)) if frequencies else None,
        },
        flush=True,
    )
    assert p95_gap < 0.25, p95_gap


@contextmanager
def _native_audio():
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import shutdown as shutdown_readback
    from solin.core.scenes.libobs_media_source import LibobsMediaSource

    with TemporaryDirectory() as directory:
        runtime = ObsRuntime()
        media = LibobsMediaSource(runtime)
        try:
            runtime.ensure_started(width=64, height=64, fps=60)
            yield runtime, media, Path(directory) / "tone.wav"
        finally:
            try:
                media.close()
            finally:
                shutdown_readback()
                runtime.shutdown()


class _AVSample(NamedTuple):
    timestamp_ns: int
    observed_at: float


class _AVVideoFrame(NamedTuple):
    number: int | None
    sample: _AVSample


class _AVOutput(NamedTuple):
    signals: dict[str, _AVSample]
    video_frames: tuple[_AVVideoFrame, ...]


def _observe_av_output(runtime, media, resources, *, resumed: Event | None = None):
    """Observe fixture transitions and optional recovery in OBS program output."""
    from pylibobs import VideoFormat
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_raw_audio_callback,
        add_raw_video_callback,
        remove_raw_audio_callback,
        remove_raw_video_callback,
    )
    from solin.core.media.obs_runtime import MONITORING_NONE

    runtime.set_source_monitoring(media.source, MONITORING_NONE)
    media.source.audio_mixers = 1
    channel = runtime.acquire_channel()
    resources.callback(runtime.release_channel, channel)
    runtime.set_channel_source(channel, media.source)
    info = ffi.new("struct obs_audio_info *")
    assert get_lib().obs_get_audio_info(info)
    sample_rate = int(info.samples_per_sec)
    signals: dict[str, _AVSample] = {}
    video_frames: list[_AVVideoFrame] = []
    lock = Lock()

    def capture_audio(_mix, planes, frames, timestamp_ns):
        after_release = resumed is not None and resumed.is_set()
        observed_at = time.monotonic()
        if not planes or not planes[0] or not frames:
            return
        pcm = array("f", planes[0])
        if max(pcm, default=0) < 0.01:
            return
        crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:], strict=False))
        if crossings < 2:
            return
        # The block can start or end between crossings. Allow one cycle of error.
        frequency_step = sample_rate / frames
        frequency = crossings * frequency_step
        if abs(frequency - AV_LOW_TONE_HZ) <= frequency_step:
            tone = "low_tone"
        elif abs(frequency - AV_HIGH_TONE_HZ) <= frequency_step:
            tone = "high_tone"
        else:
            return
        with lock:
            sample = _AVSample(timestamp_ns, observed_at)
            signals.setdefault(tone, sample)
            if after_release:
                signals.setdefault("resumed_audio", sample)

    def capture_video(planes, strides, width, height, _format, timestamp_ns):
        sample = _AVSample(timestamp_ns, time.monotonic())
        pixels, stride = planes[0], strides[0]
        offset = (height // 2) * stride + (width // 2) * 4
        blue, _green, red, _alpha = pixels[offset:offset + 4]
        # OBS program output may pass through YUV conversion.
        if red > 200 and blue < 20:
            color = "red_frame"
        elif blue > 200 and red < 20:
            color = "blue_frame"
        else:
            color = None
        frame_number = None
        if resumed is not None and color is not None:
            tile_width = width // AV_FRAME_NUMBER_BITS
            number_start = (AV_FRAME_NUMBER_ROWS // 2) * stride + (tile_width // 2) * 4
            frame_number = sum(
                (pixels[number_start + bit * tile_width * 4] >= 128) << bit
                for bit in range(AV_FRAME_NUMBER_BITS)
            )
        with lock:
            if color is not None:
                signals.setdefault(color, sample)
            if resumed is not None:
                video_frames.append(_AVVideoFrame(frame_number, sample))

    def snapshot():
        with lock:
            return _AVOutput(signals.copy(), tuple(video_frames))

    audio = add_raw_audio_callback(capture_audio, channels=1)
    resources.callback(remove_raw_audio_callback, audio)
    video = add_raw_video_callback(capture_video, format=VideoFormat.BGRA, width=64, height=64)
    resources.callback(remove_raw_video_callback, video)
    return snapshot


def _av_sync():
    with _native_media() as (runtime, media, path), ExitStack() as resources:
        write_av_clip(path, number_frames=True)
        assert media.open(str(path))
        output = _observe_av_output(runtime, media, resources)
        assert _wait_for(
            lambda: _playing(media, 600) and {"low_tone", "red_frame"} <= output().signals.keys(),
            timeout=8,
        ), "Program output did not establish both initial A/V signals"
        before_update = output().signals
        assert not {"high_tone", "blue_frame"} & before_update.keys(), before_update
        acknowledged = Event()
        resources.callback(runtime.observe_source_updates(media.source, acknowledged.set))
        media.set_speed(150)
        assert acknowledged.wait(0.25), "The native rate update was not acknowledged"
        assert _poll(media) < 2000, "The transition preceded the rate update"
        assert _wait_for(
            lambda: {"high_tone", "blue_frame"} <= output().signals.keys(), timeout=5,
        ), (
            "Program output did not reach both 2 s transitions", output().signals,
        )
        transitions = output().signals
        skew = (transitions["high_tone"].timestamp_ns - transitions["blue_frame"].timestamp_ns) / 1e9
        print("A/V output transition skew", skew, flush=True)
        assert abs(skew) <= 0.45, skew


def _video_recovery(
    frames: tuple[_AVVideoFrame, ...], hold_started_ns: int, released_ns: int,
) -> _AVSample:
    # Split the ordered OBS output by frame time. A queued frame can arrive
    # after HTTP release, and OBS can emit copies of it with newer timestamps.
    frozen_number = next(
        (frame.number for frame in reversed(frames) if frame.sample.timestamp_ns <= hold_started_ns),
        None,
    )
    assert frozen_number is not None, "Program output did not establish a frame before the hold"
    held_numbers = [
        frame.number for frame in frames
        if hold_started_ns < frame.sample.timestamp_ns <= released_ns
    ]
    assert held_numbers, "Program output did not deliver any frames during the hold"
    assert all(number == frozen_number for number in held_numbers), (
        "Program output changed during the hold", frozen_number, held_numbers,
    )
    resumed = next(
        (
            frame.sample for frame in frames
            if frame.sample.timestamp_ns > released_ns
            and frame.number is not None and frame.number > frozen_number
        ),
        None,
    )
    assert resumed is not None, "Program output did not advance after the HTTP release"
    return resumed


def _av_starvation(speed):
    from solin.core.scenes.libobs_media_source import STATE_PLAYING

    with _native_media() as (runtime, media, path), ExitStack() as stack:
        write_av_clip(path, duration=12, number_frames=True)
        stall, stalled, release = Event(), Event(), Event()
        url = stack.enter_context(
            _serve_clip(path, stall_after=stall, stalled=stalled, release_read=release)
        )
        stack.callback(media.close)
        stack.callback(release.set)
        assert media.open(url, speed_percent=speed)
        output = _observe_av_output(runtime, media, stack, resumed=release)
        assert _wait_for(lambda: _playing(media, 400), timeout=8), media.position_ms
        stall.set()
        assert stalled.wait(4), "Origin never reached its read barrier"
        previous = _poll(media)
        unchanged_since = time.monotonic()

        def frozen():
            nonlocal previous, unchanged_since
            current = _poll(media)
            if current != previous:
                previous, unchanged_since = current, time.monotonic()
            return time.monotonic() - unchanged_since >= 0.5

        assert _wait_for(frozen, timeout=6), (media.state, media.position_ms)
        frozen_at = _poll(media)
        hold_started_ns = runtime.context.video_frame_time_ns
        frame_before = _capture_prepared_source(runtime, media)["center_bgra"]
        assert frame_before == _RED_BGRA.hex(), (frozen_at, frame_before)
        time.sleep(1.5)
        assert abs(_poll(media) - frozen_at) <= 100, (frozen_at, media.position_ms)
        assert _capture_prepared_source(runtime, media)["center_bgra"] == frame_before
        before_release = output().signals
        assert {"low_tone", "red_frame"} <= before_release.keys(), before_release
        assert not {"high_tone", "blue_frame"} & before_release.keys(), (
            "Playback reached the transition before the HTTP stall", before_release,
        )

        released = time.monotonic()
        release.set()
        # This is the graphics clock used to timestamp program frames, not the
        # wall-clock time at which a pending video callback happens to run.
        released_video_ns = runtime.context.video_frame_time_ns
        assert _wait_for(lambda: _poll(media) > frozen_at + 100, timeout=5), (
            media.state,
            media.position_ms,
        )
        assert media.state == STATE_PLAYING, media.state
        time.sleep(0.4)
        elapsed = time.monotonic() - released
        resumed_at = _poll(media)
        assert resumed_at - frozen_at <= elapsed * speed / 100 * 1000 + 400, (
            frozen_at,
            resumed_at,
            elapsed,
        )

        expected = {"resumed_audio", "high_tone", "blue_frame"}
        assert _wait_for(lambda: expected <= output().signals.keys(), timeout=3), (
            "Program output did not reach both transitions after release", output().signals,
        )
        program = output()
        transitions = program.signals
        audio_resume = transitions["resumed_audio"].observed_at - released
        resumed_video = _video_recovery(program.video_frames, hold_started_ns, released_video_ns)
        video_resume = resumed_video.observed_at - released
        video_transition = transitions["blue_frame"].observed_at - released
        # Match the same marker using OBS timestamps, independent of callback delivery.
        skew = (transitions["high_tone"].timestamp_ns - transitions["blue_frame"].timestamp_ns) / 1e9
        print(
            "A/V starvation recovery",
            {
                "speed": speed,
                "frozen_ms": frozen_at,
                "resumed_ms": resumed_at,
                "elapsed": elapsed,
                "audio_resume": audio_resume,
                "video_resume": video_resume,
                "video_transition": video_transition,
                "transition_skew": skew,
            },
            flush=True,
        )
        assert 0 <= audio_resume < 0.75, audio_resume
        assert 0 <= video_resume < 2.0, video_resume
        assert abs(skew) <= 0.5, skew


def _assert_audio_continuity(recorded, sample_rate, speed):
    assert len(recorded) >= 3, (speed, len(recorded))
    silent_frames = 0
    for block in recorded:
        pcm = array("f", block[4])
        assert len(pcm) == block[2], (speed, len(pcm), block[2])
        silent = block[3] or not pcm or max(abs(value) for value in pcm) < 0.01
        silent_frames = silent_frames + block[2] if silent else 0
        assert silent_frames / sample_rate < 0.2, (speed, silent_frames)
    for previous, current in zip(recorded, recorded[1:], strict=False):
        gap = (current[1] - previous[1]) / 1e9 - previous[2] / sample_rate
        assert -0.025 <= gap <= 0.15, (speed, gap, previous[:4], current[:4])
        assert current[0] - previous[0] < 0.25, (speed, previous[0], current[0])


def _assert_no_silent_blocks(recorded):
    for index, (_arrival, _timestamp, frames, muted, data) in enumerate(recorded):
        assert not muted and any(sample != 0 for sample in array("f", data)), (
            "Silent audio block",
            index,
            len(recorded),
            frames,
            muted,
            sum(block[2] for block in recorded[:index]),
        )


def _audio_tail(speed):
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )
    from solin.core.scenes.libobs_media_source import STATE_ENDED

    with _native_audio() as (_runtime, media, path):
        duration = 2 if speed == 25 else 5
        _write_tone(path, seconds=duration)
        assert media.open(str(path), speed_percent=speed)
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        sample_rate = int(info.samples_per_sec)
        blocks = []
        lock = Lock()

        def capture(planes, frames, timestamp_ns, muted):
            with lock:
                blocks.append(
                    (time.monotonic(), timestamp_ns, frames, muted, planes[0] if planes else b"")
                )

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
            expected = duration * 100 / speed
            assert _wait_for(
                lambda: (_poll(media), media.state)[1] == STATE_ENDED, timeout=expected + 4
            ), (
                media.state,
                media.position_ms,
            )
            # Collect buffers already in flight at the terminal transition.
            time.sleep(0.25)
            with lock:
                recorded = list(blocks)
            assert recorded, "No audio was delivered"
            frames = sum(block[2] for block in recorded)
            print(
                "non-unity EOF",
                {"speed": speed, "frames": frames, "expected_seconds": expected},
                flush=True,
            )
            # Registration and the final decoded packet can each straddle a
            # fraction of a second; require the filter to drain the tail.
            assert abs(frames / sample_rate - expected) <= 0.2, (speed, frames, expected)
            _assert_audio_continuity(recorded, sample_rate, speed)
            _assert_no_silent_blocks(recorded)
        finally:
            remove_source_audio_capture_callback(media.source, callback)


def _rapid_audio_rates():
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )

    with _native_audio() as (runtime, media, path):
        _write_rate_pattern(path)
        assert media.open(str(path))
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        sample_rate = int(info.samples_per_sec)
        blocks = []
        lock = Lock()

        def capture(planes, frames, timestamp_ns, muted):
            with lock:
                blocks.append(
                    (time.monotonic(), timestamp_ns, frames, muted, planes[0] if planes else b"")
                )

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
            assert _wait_for(lambda: _playing(media, 500), timeout=8), media.position_ms
            with lock:
                blocks.clear()
            time.sleep(0.15)
            acknowledgments = []
            for speed, hold in ((25, 0.02), (200, 0.04), (150, 0.08)):
                acknowledged = Event()
                observed = []

                def updated(observed=observed, acknowledged=acknowledged):
                    observed.append(time.monotonic())
                    acknowledged.set()

                disconnect = runtime.observe_source_updates(media.source, updated)
                try:
                    requested = time.monotonic()
                    media.set_speed(speed)
                    assert acknowledged.wait(0.25), (speed, "native update was not acknowledged")
                    acknowledgments.append((speed, requested, observed[0]))
                finally:
                    disconnect()
                time.sleep(hold)
            samples = _measure_progress(media, 1.0)
            time.sleep(1.0)
            with lock:
                recorded = list(blocks)
            print("rapid audio rate acknowledgments", acknowledgments, flush=True)
            _audio_rate_observation(recorded, sample_rate, acknowledgments[-1][2])
            _assert_audio_continuity(recorded, sample_rate, "rapid")
            _assert_no_silent_blocks(recorded)
            _assert_rate(samples, 150)
        finally:
            remove_source_audio_capture_callback(media.source, callback)


def _audio(origin):
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )
    from solin.core.scenes.libobs_media_source import STATE_ENDED, STATE_PAUSED

    with _native_media() as (_runtime, media, path), ExitStack() as stack:
        path = path.with_suffix(".wav")
        _write_tone(path)
        requests = []
        location = (
            stack.enter_context(_serve_clip(path, requests=requests))
            if origin == "http"
            else str(path)
        )
        stack.callback(media.close)
        assert media.open(location)
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        sample_rate = int(info.samples_per_sec)
        blocks = []
        lock = Lock()

        def capture(planes, frames, timestamp_ns, muted):
            with lock:
                blocks.append(
                    (time.monotonic(), timestamp_ns, frames, muted, planes[0] if planes else b"")
                )

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
            assert _wait_for(lambda: _playing(media, 500), timeout=8), media.position_ms
            request_count = len(requests)
            for speed in (100, 50, 150, 200, 25, 100):
                with lock:
                    blocks.clear()
                time.sleep(0.3)
                media.set_speed(speed)
                time.sleep(0.8)
                with lock:
                    transition = list(blocks)
                    blocks.clear()
                _assert_audio_continuity(transition, sample_rate, speed)
                duration = 1.5 if speed == 25 else 1.0
                recorded, pcm = _measure_audio_progress(
                    media, blocks, lock, sample_rate, speed, duration,
                )
                assert not any(block[3] for block in recorded), speed
                rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
                crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:], strict=False))
                pitch = crossings * sample_rate / len(pcm)
                print(
                    "audio observation",
                    {"speed": speed, "pitch": pitch, "rms": rms, "blocks": len(recorded)},
                    flush=True,
                )
                assert 950 <= pitch <= 1050, (speed, pitch)
                assert rms > 0.1, (speed, rms)
                _assert_audio_continuity(recorded, sample_rate, speed)
                assert len(requests) == request_count, requests

            media.pause()
            assert _wait_for(lambda: media.state == STATE_PAUSED), media.state
            paused_at = _poll(media)
            for speed in (200, 25, 150, 50):
                media.set_speed(speed)
            time.sleep(0.4)
            assert media.state == STATE_PAUSED, media.state
            assert abs(_poll(media) - paused_at) <= 150, (paused_at, media.position_ms)
            with lock:
                blocks.clear()
            resumed = time.monotonic()
            media.play()
            assert _wait_for(lambda: _playing(media), timeout=4)
            time.sleep(0.8)
            _assert_rate(_measure_progress(media, 1.0), 50)
            with lock:
                resumed_audio = list(blocks)
            _assert_audio_continuity(resumed_audio, sample_rate, "resume")
            assert resumed_audio[0][0] - resumed < 0.5, resumed_audio[0][0] - resumed
            assert sum(block[2] for block in resumed_audio) >= sample_rate * 1.2
            assert len(requests) == request_count, requests

            # Returning to unity must preserve the tail and its duration after seek.
            media.set_speed(100)
            time.sleep(0.8)
            media.seek(36000)
            assert _wait_for(lambda: 36000 <= _poll(media) < 37000, timeout=5), media.position_ms
            position = _poll(media)
            with lock:
                blocks.clear()
            started = time.monotonic()
            assert _wait_for(lambda: media.state == STATE_ENDED, timeout=7), (
                media.state,
                media.position_ms,
            )
            elapsed = time.monotonic() - started
            expected = (40000 - position) / 1000
            assert expected - 0.8 <= elapsed <= expected + 1.5, (position, elapsed)
            with lock:
                tail = list(blocks)
            assert sum(block[2] for block in tail) >= sample_rate * (expected - 1.0), (
                position,
                len(tail),
            )
            _assert_audio_continuity(tail, sample_rate, "tail")
        finally:
            remove_source_audio_capture_callback(media.source, callback)


def _mp3_audio(origin):
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )
    from solin.core.scenes.libobs_media_source import STATE_ERROR, STATE_PAUSED

    with _native_audio() as (_runtime, media, _path), ExitStack() as stack:
        path = Path(__file__).resolve().parents[1] / "fixtures/media_extract/audio-seek-cover.mp3"
        requests = []
        location = (
            stack.enter_context(_serve_clip(path, requests=requests))
            if origin == "http"
            else str(path)
        )
        stack.callback(media.close)
        assert media.open(location)
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        sample_rate = int(info.samples_per_sec)
        blocks = []
        lock = Lock()

        def capture(planes, frames, timestamp_ns, muted):
            with lock:
                blocks.append(
                    (time.monotonic(), timestamp_ns, frames, muted, planes[0] if planes else b"")
                )

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
            assert _wait_for(lambda: _playing(media, 400), timeout=8), media.position_ms

            def sample_tone(speed, duration):
                recorded, pcm = _measure_audio_progress(
                    media, blocks, lock, sample_rate, speed, duration,
                )
                assert not any(block[3] for block in recorded), speed
                rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
                assert rms > 0.01, (speed, rms)
                crossings = [i for i in range(1, len(pcm)) if pcm[i - 1] <= 0 < pcm[i]]
                periods = [
                    b - a
                    for a, b in zip(crossings, crossings[1:], strict=False)
                    if sample_rate / 800 <= b - a <= sample_rate / 250
                ]
                assert len(periods) >= 100, (speed, len(periods))
                frequency = sample_rate / median(periods)
                print(
                    "MP3 audio observation",
                    {"origin": origin, "speed": speed, "pitch": frequency, "frames": len(pcm)},
                    flush=True,
                )
                assert 420 <= frequency <= 460, (speed, frequency)
                _assert_audio_continuity(recorded, sample_rate, speed)
                return frequency

            baseline = sample_tone(100, 0.7)
            request_count = len(requests)
            for speed, duration in ((150, 0.8), (25, 1.3)):
                media.set_speed(speed)
                time.sleep(0.8)
                pitch = sample_tone(speed, duration)
                assert abs(pitch / baseline - 1) <= 0.03, (baseline, speed, pitch)
                assert len(requests) == request_count, requests

            media.set_speed(100)
            time.sleep(0.5)
            assert len(requests) == request_count, requests
            media.seek(1200)
            assert _wait_for(lambda: 1000 <= _poll(media) <= 2000, timeout=5), (
                media.state,
                media.position_ms,
            )
            assert media.state != STATE_ERROR, media.state
            after_seek_requests = len(requests)
            media.pause()
            assert _wait_for(lambda: media.state == STATE_PAUSED), media.state
            paused_at = _poll(media)
            time.sleep(0.35)
            assert media.state == STATE_PAUSED and abs(_poll(media) - paused_at) <= 100
            assert len(requests) == after_seek_requests, requests
            media.play()
            assert _wait_for(lambda: _playing(media, paused_at + 100), timeout=4), media.state
            time.sleep(0.5)
            pitch = sample_tone(100, 0.7)
            assert abs(pitch / baseline - 1) <= 0.03, (baseline, pitch)
            assert media.state != STATE_ERROR, media.state
            assert len(requests) == after_seek_requests, requests
        finally:
            remove_source_audio_capture_callback(media.source, callback)


@pytest.mark.parametrize("observer_delay", [0, 0.35])
@pytest.mark.parametrize("audio_pacing", [0.5, 1, 2])
def test_audio_measurement_uses_elapsed_capture_window(monkeypatch, observer_delay, audio_pacing):
    now = 10.0
    blocks = []
    sample_rate = 48000

    def measure(_media, duration):
        nonlocal now
        elapsed = duration + observer_delay
        frames = round(sample_rate * elapsed * audio_pacing)
        blocks.append((now, 0, frames, False, array("f", [0.25] * frames).tobytes()))
        samples = [(now, 0), (now + duration, round(duration * 1000))]
        now += elapsed
        return samples

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(sys.modules[__name__], "_measure_progress", measure)
    if audio_pacing == 1:
        _measure_audio_progress(None, blocks, Lock(), sample_rate, 100, 0.7)
    else:
        with pytest.raises(AssertionError):
            _measure_audio_progress(None, blocks, Lock(), sample_rate, 100, 0.7)


def _run(scenario):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "faulthandler",
            "-m",
            "tests.integration.test_native_media_rate",
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-80:])
    assert result.returncode == 0, (
        f"Native scenario {scenario!r} exited {result.returncode}:\n{diagnostics}"
    )


@pytest.mark.parametrize("origin", ["local", "http"])
def test_native_rate_change_preserves_playback_and_pause(origin):
    _run(origin)


def test_native_rate_survives_seek_eof_and_restart():
    _run("seek")


def test_native_rate_preserves_audio_video_sync():
    _run("av")


@pytest.mark.parametrize("speed", [100, 150])
def test_native_av_starvation_resumes_without_clock_catchup(speed):
    _run(f"av-starvation-{speed}")


@pytest.mark.parametrize("origin", ["local", "http"])
def test_native_rate_preserves_pitch_and_continuous_audio(origin):
    _run(f"audio-{origin}")


@pytest.mark.parametrize("origin", ["local", "http"])
def test_native_mp3_rate_preserves_pitch_seek_and_pause(origin):
    _run(f"mp3-{origin}")


@pytest.mark.parametrize("speed", [25, 200])
def test_native_nonunity_audio_drains_at_eof(speed):
    _run(f"audio-tail-{speed}")


def test_native_rapid_rate_changes_keep_audio_continuous():
    _run("audio-rapid")


if __name__ == "__main__":
    scenario = sys.argv[1]
    if scenario in ("local", "http"):
        _transport(scenario)
    elif scenario == "seek":
        _seek_and_restart()
    elif scenario == "av":
        _av_sync()
    elif scenario.startswith("av-starvation-"):
        _av_starvation(int(scenario.removeprefix("av-starvation-")))
    elif scenario.startswith("audio-tail-"):
        _audio_tail(int(scenario.removeprefix("audio-tail-")))
    elif scenario == "audio-rapid":
        _rapid_audio_rates()
    elif scenario.startswith("mp3-"):
        _mp3_audio(scenario.removeprefix("mp3-"))
    elif scenario.startswith("audio-"):
        _audio(scenario.removeprefix("audio-"))
    else:
        raise ValueError(scenario)
