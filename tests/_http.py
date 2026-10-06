"""Real HTTP test servers confined to loopback, without hostname discovery."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(HTTPServer):
    def __init__(self, handler: type[BaseHTTPRequestHandler], *, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), handler)

    def server_bind(self) -> None:
        # HTTPServer otherwise resolves its bound address with getfqdn(). That
        # metadata lookup can request local-network access on macOS.
        # These servers exercise HTTP on loopback; they do not discover hosts.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]
