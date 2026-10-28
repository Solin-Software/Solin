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
import wave

import pytest

from tests.integration.test_native_media_startup import (
    _BLUE_BGRA,
    _FPS,
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
        samples.append((time.monotonic(), _poll(media)))
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


def _change_rate_and_measure(runtime, media, speed, previous_speed, duration):
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
        samples = [(requested_at, before)]
        deadline = requested_at + 0.25
        while not acknowledged.is_set() and time.monotonic() < deadline:
            samples.append((time.monotonic(), _poll(media)))
            time.sleep(0.005)
        assert acknowledged.is_set(), (speed, "native update was not acknowledged")
        ack_at = ack_times[0]
        after_ack = _measure_progress(media, duration)
        samples.extend(after_ack)
        latency = ack_at - requested_at
        print("rate update acknowledgment", {"speed": speed, "latency": latency}, flush=True)
        assert latency <= 0.25, latency
        # One video tick can still use the old rate; one frame accounts for
        # the quantized native media position at the observation boundary.
        max_initial_advance = previous_speed / 100 * 1000 / _FPS + 100
        assert after_ack[0][1] - before <= max_initial_advance, (
            before,
            after_ack[0],
            latency,
            max_initial_advance,
        )
        assert min(position for _, position in samples) >= before - 100, (before, samples)
        assert all(b >= a - 100 for (_, a), (_, b) in zip(samples, samples[1:], strict=False)), (
            samples
        )
        _assert_rate(after_ack, speed)
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
        for previous, speed, duration in ((100, 150, 1.0), (150, 200, 0.8), (200, 25, 1.5)):
            _change_rate_and_measure(runtime, media, speed, previous, duration)
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


def _write_av_clip(path, *, duration=5, periodic=False, constant_frequency=None):
    """Interleave uncompressed video and PCM, optionally keeping one audio tone."""
    width, fps, rate = 64, 10, 48000
    frame_size = width * width * 3
    frame_count = duration * fps
    samples_per_frame = rate // fps

    def chunk(kind, data):
        return kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)

    header = struct.pack(
        "<14I",
        1000000 // fps,
        frame_size * fps + rate * 2,
        0,
        0x10,
        frame_count,
        0,
        2,
        frame_size,
        width,
        width,
        0,
        0,
        0,
        0,
    )
    video = struct.pack(
        "<4s4sIHH8I4h",
        b"vids",
        b"DIB ",
        0,
        0,
        0,
        0,
        1,
        fps,
        0,
        frame_count,
        frame_size,
        0xFFFFFFFF,
        0,
        0,
        0,
        width,
        width,
    )
    bitmap = struct.pack("<IiiHHIIiiII", 40, width, width, 1, 24, 0, frame_size, 0, 0, 0, 0)
    audio = struct.pack(
        "<4s4sIHH8I4h",
        b"auds",
        b"\0" * 4,
        0,
        0,
        0,
        0,
        2,
        rate * 2,
        0,
        rate * duration,
        samples_per_frame * 2,
        0xFFFFFFFF,
        2,
        0,
        0,
        0,
        0,
    )
    wave_format = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    video_list = chunk(b"LIST", b"strl" + chunk(b"strh", video) + chunk(b"strf", bitmap))
    audio_list = chunk(b"LIST", b"strl" + chunk(b"strh", audio) + chunk(b"strf", wave_format))
    headers = chunk(b"LIST", b"hdrl" + chunk(b"avih", header) + video_list + audio_list)
    payload, index, offset = [], [], 4
    for frame in range(frame_count):
        blue = bool((frame // fps) % 2) if periodic else frame >= 2 * fps
        pixel = b"\xff\0\0" if blue else b"\0\0\xff"
        frequency = (
            constant_frequency if constant_frequency is not None else (1000 if blue else 400)
        )
        pcm = b"".join(
            struct.pack("<h", round(16000 * math.sin(2 * math.pi * frequency * i / rate)))
            for i in range(samples_per_frame)
        )
        for kind, data in ((b"00db", pixel * (width * width)), (b"01wb", pcm)):
            entry = chunk(kind, data)
            payload.append(entry)
            index.append(struct.pack("<4sIII", kind, 0x10, offset, len(data)))
            offset += len(entry)
    data = b"AVI " + headers + chunk(b"LIST", b"movi" + b"".join(payload))
    data += chunk(b"idx1", b"".join(index))
    path.write_bytes(b"RIFF" + struct.pack("<I", len(data)) + data)


def _av_sync():
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )

    with _native_media() as (runtime, media, path):
        _write_av_clip(path)
        assert media.open(str(path))
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        high_tone = []
        lock = Lock()

        def capture(planes, frames, _timestamp_ns, muted):
            if muted or not planes or not planes[0] or not frames:
                return
            pcm = array("f", planes[0])
            crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:], strict=False))
            if crossings * int(info.samples_per_sec) / frames >= 750:
                with lock:
                    high_tone.append(time.monotonic())

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
            assert _wait_for(lambda: _playing(media, 600), timeout=8)
            media.set_speed(150)
            deadline = time.monotonic() + 5
            blue_at = None
            while time.monotonic() < deadline:
                if _capture_prepared_source(runtime, media)["center_bgra"] == _BLUE_BGRA.hex():
                    blue_at = time.monotonic()
                    break
            assert blue_at is not None, "Video did not reach the 2 s transition"
            assert _wait_for(lambda: bool(high_tone), timeout=3), (
                "Audio did not reach its 2 s transition"
            )
            with lock:
                audio_at = high_tone[0]
            skew = audio_at - blue_at
            print("A/V transition skew", skew, flush=True)
            assert abs(skew) <= 0.45, skew
        finally:
            remove_source_audio_capture_callback(media.source, callback)


def _av_starvation(speed):
    from pylibobs._ffi import ffi, get_lib
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )
    from solin.core.scenes.libobs_media_source import STATE_PLAYING

    with _native_media() as (runtime, media, path), ExitStack() as stack:
        _write_av_clip(path, duration=12, periodic=True)
        stall, stalled, release = Event(), Event(), Event()
        url = stack.enter_context(
            _serve_clip(path, stall_after=stall, stalled=stalled, release_read=release)
        )
        stack.callback(media.close)
        stack.callback(release.set)
        assert media.open(url, speed_percent=speed)
        info = ffi.new("struct obs_audio_info *")
        assert get_lib().obs_get_audio_info(info)
        events = []
        lock = Lock()

        def capture(planes, frames, _timestamp_ns, muted):
            if muted or not planes or not planes[0] or not frames:
                return
            pcm = array("f", planes[0])
            crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:], strict=False))
            with lock:
                events.append(
                    (time.monotonic(), crossings * int(info.samples_per_sec) / frames >= 750)
                )

        callback = add_source_audio_capture_callback(media.source, capture, channels=1)
        try:
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
            assert frozen_at < 10000, frozen_at
            frame_before = _capture_prepared_source(runtime, media)["center_bgra"]
            assert frame_before in {_RED_BGRA.hex(), _BLUE_BGRA.hex()}, frame_before
            time.sleep(1.5)
            assert abs(_poll(media) - frozen_at) <= 100, (frozen_at, media.position_ms)
            assert _capture_prepared_source(runtime, media)["center_bgra"] == frame_before
            with lock:
                events.clear()
            released = time.monotonic()
            release.set()
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

            deadline = time.monotonic() + 3
            video_at = None
            while time.monotonic() < deadline:
                frame = _capture_prepared_source(runtime, media)["center_bgra"]
                if frame != frame_before:
                    video_at = time.monotonic()
                    break
            assert video_at is not None, "Video did not resume to its next transition"
            assert video_at - released < 2.0, video_at - released
            target = frame == _BLUE_BGRA.hex()

            def matching_audio():
                with lock:
                    return next((when for when, blue in events if blue == target), None)

            assert _wait_for(lambda: matching_audio() is not None, timeout=3), (
                "Audio did not resume"
            )
            audio_at = matching_audio()
            assert audio_at is not None
            with lock:
                first_audio_at = events[0][0]
                audio_blocks = len(events)
            assert first_audio_at - released < 0.75, first_audio_at - released
            print(
                "A/V starvation recovery",
                {
                    "speed": speed,
                    "frozen_ms": frozen_at,
                    "resumed_ms": resumed_at,
                    "elapsed": elapsed,
                    "audio_resume": first_audio_at - released,
                    "video_transition": video_at - released,
                    "audio_blocks": audio_blocks,
                    "transition_skew": audio_at - video_at,
                },
                flush=True,
            )
            assert abs(audio_at - video_at) <= 0.5, (frozen_at, audio_at, video_at)
        finally:
            remove_source_audio_capture_callback(media.source, callback)


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
                samples = _measure_progress(media, duration)
                with lock:
                    recorded = list(blocks)
                _assert_rate(samples, speed)
                pcm = array("f", b"".join(block[4] for block in recorded))
                assert len(pcm) >= sample_rate * duration * 0.7, (speed, len(pcm))
                assert len(pcm) <= sample_rate * duration * 1.3, (speed, len(pcm))
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
                with lock:
                    blocks.clear()
                samples = _measure_progress(media, duration)
                with lock:
                    recorded = list(blocks)
                _assert_rate(samples, speed)
                pcm = array("f", b"".join(block[4] for block in recorded))
                assert sample_rate * duration * 0.7 <= len(pcm) <= sample_rate * duration * 1.3, (
                    speed,
                    len(pcm),
                    duration,
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
