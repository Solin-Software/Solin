from __future__ import annotations

from contextlib import closing
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from tests._http import LoopbackHTTPServer, ThreadingLoopbackHTTPServer
from tests.integration import test_native_media_startup as media_fixture


@pytest.mark.parametrize("server_type", [LoopbackHTTPServer, ThreadingLoopbackHTTPServer])
def test_http_test_server_serves_loopback_without_hostname_discovery(monkeypatch, server_type):
    def forbidden_discovery(*_args):
        raise AssertionError("HTTP test fixtures must not discover local network hosts")

    monkeypatch.setattr(socket, "getfqdn", forbidden_discovery)
    monkeypatch.setattr(socket, "gethostname", forbidden_discovery)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - HTTP handler contract
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"test")

        def log_message(self, *_args):
            return

    with server_type(Handler) as server:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_name == "localhost"
        assert server.server_port > 0
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as client:
                client.request("GET", "/test")
                response = client.getresponse()
                assert response.status == 200
                assert response.read() == b"test"
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive(), "HTTP fixture did not stop"


@pytest.fixture
def paced_clip_clock(monkeypatch):
    """Exercise the real HTTP handler without wall-clock timing assertions."""
    completed = threading.Event()
    errors = []

    class ObservedServer(ThreadingLoopbackHTTPServer):
        def finish_request(self, request, client_address):
            try:
                super().finish_request(request, client_address)
            finally:
                completed.set()

        def handle_error(self, request, client_address):
            errors.append(sys.exception())

    clock = SimpleNamespace(now=137.0, oversleep=0.0, sleeps=[])

    def sleep(seconds):
        assert seconds > 0
        clock.sleeps.append(seconds)
        clock.now += seconds + clock.oversleep

    # Replace this fixture module's clock, not threading/socket's shared time module.
    monkeypatch.setattr(media_fixture, "time", SimpleNamespace(
        monotonic=lambda: clock.now, sleep=sleep,
    ))
    monkeypatch.setattr("tests._http.ThreadingLoopbackHTTPServer", ObservedServer)
    yield clock, completed
    assert not errors, errors


@pytest.mark.parametrize("oversleep", [0.002, 0.02])
def test_media_http_pacing_recovers_delayed_wakeups(tmp_path, paced_clip_clock, oversleep):
    clock, completed = paced_clip_clock
    clock.oversleep = oversleep
    data = bytes(range(256)) * 1024
    path = tmp_path / "clip.bin"
    path.write_bytes(data)
    start, end = 123, 123 + 4096 * 48 + 37 - 1
    requests = []
    began = clock.now
    with media_fixture._serve_clip(path, stall_after=threading.Event(), requests=requests) as url:
        host, route = url.removeprefix("http://").split("/", 1)
        with closing(HTTPConnection(host, timeout=2)) as client:
            client.request("GET", f"/{route}", headers={"Range": f"bytes={start}-{end}"})
            response = client.getresponse()
            assert response.status == 206
            assert response.getheader("Content-Range") == f"bytes {start}-{end}/{len(data)}"
            assert response.read() == data[start:end + 1]
            assert completed.wait(2), "Paced response did not finish"
    assert requests == [start]
    duration = (end - start + 1) / 409600
    # The first block is immediate; wakeup error may affect the final deadline,
    # but must not accumulate once per block.
    assert duration - 0.01 - 1e-9 <= clock.now - began <= duration + oversleep + 1e-9
    assert clock.sleeps, "The controlled origin must still pace its response"


def test_media_http_pacing_excludes_the_deliberate_stall(tmp_path, paced_clip_clock):
    clock, completed = paced_clip_clock
    clock.oversleep = 0.002
    data = b"pcm!" * (4096 * 16)
    path = tmp_path / "clip.bin"
    path.write_bytes(data)
    stall, stalled = threading.Event(), threading.Event()
    stall.set()
    sleeps_after_release = []

    class ReleaseRead:
        released = False

        def is_set(self):
            return self.released

        def wait(self, _timeout):
            assert stalled.is_set(), "The origin must publish its barrier before waiting"
            if not self.released:
                clock.now += 1.5
                self.released = True
                sleeps_after_release.append(len(clock.sleeps))
            return True

        def set(self):
            self.released = True

    began = clock.now
    with media_fixture._serve_clip(
        path, stall_after=stall, stalled=stalled, release_read=ReleaseRead(),
    ) as url:
        host, route = url.removeprefix("http://").split("/", 1)
        with closing(HTTPConnection(host, timeout=2)) as client:
            client.request("GET", f"/{route}")
            response = client.getresponse()
            assert response.read() == data
            assert completed.wait(2), "Released response did not finish"
    assert sleeps_after_release == [0]
    assert clock.sleeps, "The deliberate stall must not buy an unpaced catch-up burst"
    duration = len(data) / 409600
    assert duration - 0.01 - 1e-9 <= clock.now - began - 1.5 <= duration + clock.oversleep + 1e-9


def test_media_http_pacing_preserves_disconnect_offset_and_range_resume(tmp_path, paced_clip_clock):
    clock, completed = paced_clip_clock
    data = bytes(range(256)) * 128
    path = tmp_path / "clip.bin"
    path.write_bytes(data)
    interrupt = threading.Event()
    interrupt.set()
    requests, offsets = [], []
    start = 77
    with media_fixture._serve_clip(
        path, disconnect_once=True, disconnect_after=interrupt,
        requests=requests, disconnect_offsets=offsets,
    ) as url:
        host, route = url.removeprefix("http://").split("/", 1)
        with closing(HTTPConnection(host, timeout=2)) as client:
            client.request("GET", f"/{route}", headers={"Range": f"bytes={start}-"})
            response = client.getresponse()
            with pytest.raises(IncompleteRead) as failed:
                response.read()
            assert failed.value.partial == data[start:start + 4096]
            assert completed.wait(2)
        assert offsets == [start + 4096]
        completed.clear()
        with closing(HTTPConnection(host, timeout=2)) as client:
            client.request("GET", f"/{route}", headers={"Range": f"bytes={offsets[0]}-"})
            response = client.getresponse()
            assert response.status == 206
            assert response.read() == data[offsets[0]:]
            assert completed.wait(2)
    assert requests == [start, offsets[0]]
    assert not clock.sleeps, "Interrupted and unpaced resumed responses need no artificial delay"
