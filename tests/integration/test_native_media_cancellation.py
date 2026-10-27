"""Exercise finite-input cancellation against a real stalled HTTP origin."""

from __future__ import annotations

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
                assert media.state == STATE_STOPPED, "Cancellation became an error or natural completion"
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


if __name__ == "__main__":
    if sys.argv[1:] not in (["close"], ["stop"]):
        raise SystemExit("Run with pytest or close/stop")
    sys.path.insert(0, str(ROOT))
    _assert_stalled_input_closes(stop_first=sys.argv[1] == "stop")
