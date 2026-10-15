from __future__ import annotations

from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import socket
import threading

import pytest

from tests._http import LoopbackHTTPServer, ThreadingLoopbackHTTPServer


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
