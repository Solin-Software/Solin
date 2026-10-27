from __future__ import annotations

from contextlib import ExitStack
import subprocess
import sys
from threading import Event
import time

import pytest

from tests.integration.test_native_media_startup import (
    _native_media,
    _observation,
    _serve_clip,
    _wait_for,
)

pytestmark = pytest.mark.integration


def _assert_finite_starvation(*, contract: str) -> None:
    from solin.core.scenes.libobs_media_source import (
        STATE_BUFFERING, STATE_PAUSED, STATE_PLAYING, STATE_STOPPED,
    )

    with _native_media() as (_runtime, media, path), ExitStack() as resources:
        stall_after, stalled, release = Event(), Event(), Event()
        url = resources.enter_context(_serve_clip(
            path, stall_after=stall_after, stalled=stalled, release_read=release,
        ))
        resources.callback(media.close)
        resources.callback(release.set)
        assert media.open(url, volume_percent=0)

        def progressing():
            media.apply_startup_transport()
            return media.state == STATE_PLAYING and media.position_ms >= 200

        assert _wait_for(progressing), _observation(media)
        stall_after.set()
        assert stalled.wait(3), "The origin did not reach the controlled read boundary"

        # Wait for decoded/read-ahead bytes to drain; the server stays blocked.
        previous = media.position_ms
        unchanged_since = time.monotonic()

        def starved():
            nonlocal previous, unchanged_since
            position = media.position_ms
            if position != previous:
                previous, unchanged_since = position, time.monotonic()
            return time.monotonic() - unchanged_since >= 0.5

        assert _wait_for(starved, timeout=4), _observation(media)
        frozen_at = media.position_ms
        if contract != "clock":
            assert media.state == STATE_BUFFERING, _observation(media)
        if contract in {"stop", "stop_restart", "stop_replay"}:
            media.stop()
            assert _wait_for(lambda: media.state == STATE_STOPPED), _observation(media)
            if contract == "stop_restart":
                release.set()
                time.sleep(0.3)
                media.play()
                assert _wait_for(lambda: media.position_ms >= 500 and
                                 media.state == STATE_PLAYING), _observation(media)
                return
            if contract == "stop_replay":
                media.restart()
                release.set()
                assert _wait_for(lambda: media.position_ms >= 500 and
                                 media.state == STATE_PLAYING), _observation(media)
                return
            started = time.monotonic()
            media.close()
            assert time.monotonic() - started < 2
            return
        if contract == "pause":
            media.pause()
            assert _wait_for(lambda: media.state == STATE_PAUSED), _observation(media)
        time.sleep(1.5)
        assert abs(media.position_ms - frozen_at) <= 100, _observation(media)
        release.set()
        if contract == "pause":
            time.sleep(0.4)
            assert media.state == STATE_PAUSED, _observation(media)
            assert abs(media.position_ms - frozen_at) <= 100, _observation(media)
            media.play()
        assert _wait_for(lambda: media.position_ms > frozen_at + 100), _observation(media)
        # Recovery must continue the media clock rather than catch up to wall time.
        time.sleep(0.3)
        recovered_at = media.position_ms
        print("starvation_recovery", {"frozen_at": frozen_at, "recovered_at": recovered_at,
                                      **_observation(media)}, flush=True)
        assert recovered_at - frozen_at < 900, (frozen_at, recovered_at)
        assert media.state == STATE_PLAYING, _observation(media)


@pytest.mark.parametrize("contract", ["state", "clock", "pause", "stop", "stop_restart", "stop_replay"])
def test_finite_http_starvation_reports_buffering_and_preserves_media_time(contract):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-m",
         "tests.integration.test_native_media_buffering", contract],
        capture_output=True, text=True, timeout=25, check=False,
    )
    assert result.returncode == 0, "\n".join((result.stdout + result.stderr).splitlines()[-60:])


if __name__ == "__main__":
    _assert_finite_starvation(contract=sys.argv[1])

