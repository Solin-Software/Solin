"""Exercise foreground audio activity using the real libobs media source."""

from __future__ import annotations

from contextlib import ExitStack
import struct
import subprocess
import sys
import time

import pytest

from tests.fixtures.av_clip import write_av_clip
from tests.integration.test_native_media_startup import _native_media, _serve_clip

pytestmark = pytest.mark.integration


def _write_activity_clip(path):
    """Keep video advancing through a short pause, a long pause and a quiet tail."""
    write_av_clip(path, duration=9, constant_frequency=400)
    data = bytearray(path.read_bytes())
    offset = data.index(b"movi") + 4
    for frame in range(90):
        for expected in (b"00db", b"01wb"):
            kind = data[offset:offset + 4]
            size = struct.unpack_from("<I", data, offset + 4)[0]
            assert kind == expected
            start = offset + 8
            if kind == b"01wb" and (15 <= frame < 19 or 30 <= frame < 50 or frame >= 65):
                data[start:start + size] = bytes(size)
            offset = start + size + size % 2
    path.write_bytes(data)


def _observe_activity(origin):
    from solin.core.scenes.libobs_media_source import (
        LibobsMediaSource, STATE_ENDED, STATE_PLAYING,
    )

    with _native_media() as (runtime, _unused, path), ExitStack() as resources:
        _write_activity_clip(path)
        media = LibobsMediaSource(runtime, silence_hold_ms=800)
        resources.callback(media.close)
        requests = []
        location = (
            resources.enter_context(_serve_clip(path, accept_ranges=False, requests=requests))
            if origin == "http" else str(path)
        )
        assert media.open(location)
        observations = []
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            media.apply_startup_transport()
            state, position = media.state, media.position_ms
            silent = media.audio_is_silent(state, position)
            observations.append((state, position, silent))
            if state == STATE_ENDED:
                break
            time.sleep(0.025)
        else:
            raise AssertionError(f"Playback did not finish: {observations[-20:]}")

        if origin == "http":
            assert requests, "The player did not request the HTTP fixture"

        def observed(start, end, expected):
            window = [silent for state, position, silent in observations
                      if state == STATE_PLAYING and start <= position < end]
            assert window, (start, end, observations)
            assert all(value is expected for value in window), (start, end, window, observations)

        observed(500, 1400, False)       # initial tone
        observed(1550, 1850, False)      # 400 ms pause does not reach the hold
        observed(4300, 4800, True)       # sustained quiet
        observed(5500, 6300, False)      # tone returned
        observed(8000, 8700, True)       # quiet while video still advances
        assert observations[-1][0] == STATE_ENDED
        assert observations[-1][2] is False


@pytest.mark.parametrize("origin", ["local", "http"])
def test_foreground_audio_activity_during_playback(origin):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-m",
         "tests.integration.test_native_media_audio_activity", origin],
        capture_output=True, text=True, timeout=30, check=False,
    )
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-80:])
    assert result.returncode == 0, f"{origin} playback exited {result.returncode}:\n{diagnostics}"


if __name__ == "__main__":
    _observe_activity(sys.argv[1])
