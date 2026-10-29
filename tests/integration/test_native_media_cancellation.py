"""Native cancellation and explicit-stop input lifecycle contracts."""

from __future__ import annotations

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def _assert_stalled_input_closes(*, stop_first: bool = False) -> None:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes.libobs_media_source import LibobsMediaSource, STATE_STOPPED
    from tests._http import ThreadingLoopbackHTTPServer

    requested, release = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.set()
            # Keep FFmpeg blocked before the response headers. The origin stays
            # stalled until native teardown has finished, so cleanup cannot
            # accidentally unblock the read and conceal a missing interrupt.
            release.wait(30)
            self.close_connection = True

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingLoopbackHTTPServer(Handler)
    serving = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    serving.start()
    runtime = ObsRuntime()
    media = LibobsMediaSource(runtime)
    try:
        runtime.ensure_started(width=64, height=64, fps=10)
        assert media.open(
            f"http://127.0.0.1:{server.server_port}/stalled.wav",
            autoplay=True,
            volume_percent=0,
        )
        assert requested.wait(5), "The native input never reached the stalled origin"
        if stop_first:
            media.stop()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                media.apply_startup_transport()
                assert media.state == STATE_STOPPED, (
                    "Cancellation became an error or natural completion"
                )
                time.sleep(0.02)
        started = time.monotonic()
        media.close()
        # Source release is deferred. Drain actual native destruction too.
        runtime.shutdown()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"Finite HTTP teardown took {elapsed:.3f}s"
        print(f"Finite HTTP close and native destruction: {elapsed:.3f}s", flush=True)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)
        media.close()
        runtime.shutdown()


@pytest.mark.parametrize("operation", ["close", "stop"])
def test_finite_http_transport_cancels_a_stalled_origin(operation) -> None:
    # A regression must fail with a bounded timeout instead of wedging pytest.
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), operation],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


def _assert_stop_reopens_input(scenario: str) -> None:
    from solin.core.scenes.libobs_media_source import (
        MEDIA_SOURCE_ID,
        STATE_ERROR,
        STATE_PLAYING,
        STATE_STOPPED,
    )
    from tests.integration.test_native_media_startup import (
        _hold_tick,
        _native_media,
        _serve_clip,
        _wait_for,
    )

    with _native_media() as (runtime, _media, path), ExitStack() as resources:
        requests = []
        reject_input = Event()
        live = scenario != "local-close"
        settings = {"restart_on_activate": False, "hw_decode": False}
        if live:
            if scenario == "reconnecting":
                reject_input.set()
            url = resources.enter_context(
                _serve_clip(
                    path,
                    requests=requests,
                    abort_requests=reject_input,
                )
            )
            # Exercise the native live lifecycle explicitly. Solin correctly
            # classifies ordinary HTTP downloads as finite inputs instead.
            settings.update(is_local_file=False, input=url, reconnect_delay_sec=1)
        else:
            settings.update(is_local_file=True, local_file=str(path), close_when_inactive=True)
        source = runtime.ob.Source.create(MEDIA_SOURCE_ID, "stop-lifecycle", settings)
        resources.callback(source.release)
        source.volume = 0
        channel = runtime.acquire_channel()
        resources.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, source)

        def progressing():
            return source.media_state == STATE_PLAYING and source.media_time >= 200

        if scenario == "reconnecting":
            assert _wait_for(
                lambda: len(requests) >= 2 and source.media_state == STATE_ERROR,
                timeout=6,
            ), (source.media_state, requests)
        else:
            assert _wait_for(progressing, timeout=6), (source.media_state, requests)
            assert source.media_duration > 0

        if scenario == "live-rapid":
            before = len(requests)
            # Both commands must run before the next destruction tick, rather
            # than relying on a race between Python and the graphics thread.
            with _hold_tick(runtime):
                source.media_stop()
                source.media_play_pause(False)
            assert _wait_for(lambda: len(requests) > before, timeout=3), requests
        else:
            source.media_stop()
            assert _wait_for(
                lambda: source.media_state == STATE_STOPPED and source.media_duration == 0,
                timeout=2,
            ), ("Stop retained the input", source.media_state, source.media_duration)
            stopped_requests = len(requests)
            # A pending live reconnect has a one-second delay. Stop must remain
            # terminal until a new explicit Play, even after that delay expires.
            time.sleep(1.2)
            assert source.media_state == STATE_STOPPED, source.media_state
            assert len(requests) == stopped_requests, (stopped_requests, requests)
            reject_input.clear()
            source.media_play_pause(False)
            if live:
                assert _wait_for(lambda: len(requests) > stopped_requests, timeout=3), requests

        assert _wait_for(progressing, timeout=5), (source.media_state, source.media_time, requests)
        position = source.media_time
        assert _wait_for(lambda: source.media_time > position + 100, timeout=2), (
            "The reopened input did not continue playing",
            source.media_state,
            source.media_time,
        )


@pytest.mark.parametrize("scenario", ["local-close", "live", "live-rapid", "reconnecting"])
def test_explicit_stop_releases_input_and_play_opens_a_fresh_input(scenario) -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), scenario],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-5000:]


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    if sys.argv[1:] in (["close"], ["stop"]):
        _assert_stalled_input_closes(stop_first=sys.argv[1] == "stop")
    elif len(sys.argv) == 2 and sys.argv[1] in {
        "local-close",
        "live",
        "live-rapid",
        "reconnecting",
    }:
        _assert_stop_reopens_input(sys.argv[1])
    else:
        raise SystemExit("Run with pytest or a supported cancellation/lifecycle scenario")
