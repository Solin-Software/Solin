"""Loopback server and external-artifact HTTP qualification shared by E2E callers."""

from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from solin.bootstrap.runtime_verification import HTTP_RUNTIME_CHECKS, HTTP_RUNTIME_PAYLOAD
from tests._http import LoopbackHTTPServer
from tests.e2e._packaged_app import _signal_process_group, isolated_app_env


class _HttpRuntimeHandler(BaseHTTPRequestHandler):
    payload = HTTP_RUNTIME_PAYLOAD

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(2)

    def do_GET(self) -> None:
        if self.path == "/http-runtime":
            body = gzip.compress(self.payload, mtime=0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
        else:
            body = b"qualification status failure"
            self.send_response(503 if self.path == "/http-runtime?status=503" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _http_runtime_server(
    handler: type[_HttpRuntimeHandler] = _HttpRuntimeHandler,
) -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class RecordingHandler(handler):
        def do_GET(self) -> None:
            requests.append(self.path)
            super().do_GET()

    with LoopbackHTTPServer(RecordingHandler) as server:
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="http-runtime-qualification",
            daemon=True,
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/http-runtime", requests
        finally:
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive(), "HTTP qualification server did not stop."


def assert_packaged_http_runtime(
    exe_path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> None:
    """Qualify an external artifact; reusable after installing or replacing it.

    The executable may be an AppImage launcher. The child must make all four
    requests and report JSON; the host interpreter never runs the HTTP adapter.
    """
    executable = exe_path.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="solin-http-e2e-") as directory:
        environment = dict(env) if env is not None else isolated_app_env(Path(directory))
        for name in ("PYTHONPATH", "PYTHONHOME"):
            environment.pop(name, None)
        with _http_runtime_server() as (url, requests):
            process = subprocess.Popen(
                [str(executable), "--verify-http-runtime", url],
                cwd=executable.parent,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pytest.fail(f"Packaged HTTP qualification exceeded {timeout:g} seconds.")
            finally:
                # Own the full AppImage process group, even when its launcher
                # exits before descendants or leaves a captured pipe open.
                if os.name == "posix":
                    _signal_process_group(process.pid, signal.SIGKILL)
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                # Drain and close captured pipes after timeout cleanup as well
                # as normal completion (including Windows reader threads).
                process.communicate(timeout=5)
            diagnostics = stderr.decode("utf-8", errors="replace")[-16000:]
            assert process.returncode == 0, (
                f"Packaged HTTP qualification exited with {process.returncode}:\n"
                f"{stdout.decode('utf-8', errors='replace')}\n{diagnostics}"
            )
            try:
                result = json.loads(stdout)
            except (ValueError, UnicodeError) as error:
                pytest.fail(f"Invalid packaged HTTP qualification JSON: {error}\n{diagnostics}")
            assert result == {
                "schema": "solin.http-runtime.v1",
                "ok": True,
                "checks": list(HTTP_RUNTIME_CHECKS),
            }, f"Unexpected packaged HTTP qualification result: {result!r}\n{diagnostics}"
            assert requests == [
                "/http-runtime",
                "/http-runtime",
                "/http-runtime?status=503",
                "/http-runtime?status=503",
            ], f"The artifact did not exercise all HTTP operations: {requests!r}"
