from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT
from tests.e2e._http_runtime import (
    _HttpRuntimeHandler,
    _http_runtime_server,
    assert_packaged_http_runtime,
)


def _run_headless(
    arguments: list[str],
    *,
    cwd: Path,
    prelude: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    code = (
        "import runpy, sys; "
        "sys.modules['PySide6'] = None; "
        "sys.modules['solin.bootstrap.application'] = None; "
        + prelude
        + f"sys.argv = ['Solin', *{arguments!r}]; "
        f"runpy.run_path({str(REPO_ROOT / 'main.py')!r}, run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=env,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--verify-http-runtime"],
        ["--verify-http-runtime", "http://127.0.0.1:1", "--create-profile"],
        ["--create-profile", "--verify-http-runtime", "http://127.0.0.1:1"],
        ["--verify-http-runtime=http://127.0.0.1:1"],
        ["--verify-http-runtime", "--scene-engine-sidecar"],
        ["--scene-engine-sidecar", "--verify-http-runtime", "http://127.0.0.1:1"],
    ],
)
def test_invalid_verification_arguments_fail_as_json_before_qt(arguments, tmp_path):
    result = _run_headless(arguments, cwd=tmp_path)
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solin.http-runtime.v1",
        "ok": False,
        "error": "ValueError",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:80/runtime",
        "http://localhost:80/runtime",
        "http://example.test:80/runtime",
        "http://192.168.1.1:80/runtime",
        "http://0.0.0.0:80/runtime",
        "http://[::]:80/runtime",
        "file:///tmp/runtime",
        "http://127.0.0.1/runtime",
        "http://127.0.0.1:0/runtime",
        "http://127.0.0.1:65536/runtime",
        "http://127.0.0.1:bad/runtime",
        "http://user:password@127.0.0.1:80/runtime",
        "http://127.0.0.1:80/runtime?x=1",
        "http://127.0.0.1:80/runtime#fragment",
        " http://127.0.0.1:80/runtime",
        "http://127.0.0.1:80/\nruntime",
        "http://[::1%25lo]:80/runtime",
        "http://2130706433:80/runtime",
        "http://127.1:80/runtime",
        "",
    ],
)
def test_invalid_urls_fail_before_transport_import(url, tmp_path):
    result = _run_headless(
        ["--verify-http-runtime", url],
        cwd=tmp_path,
        prelude="sys.modules['solin.core.network.http'] = None; ",
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["error"] == "ValueError"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1:123/runtime", ("127.0.0.1", 123)),
        ("http://127.0.0.2:123/runtime", ("127.0.0.2", 123)),
        ("http://[::1]:123/runtime", ("::1", 123)),
    ],
)
def test_literal_loopback_validation(url, expected):
    from solin.bootstrap.runtime_verification import validate_loopback_url

    assert validate_loopback_url(url) == expected


def test_headless_runtime_uses_real_gzip_get_and_stream_without_user_state(tmp_path):
    environment = os.environ.copy()
    environment.update(HTTP_PROXY="http://192.0.2.1:1", http_proxy="http://192.0.2.1:1")
    with _http_runtime_server() as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path, env=environment)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        '{"schema":"solin.http-runtime.v1","ok":true,'
        '"checks":["get","stream","get_status","stream_status"]}\n'
    )
    assert requests == [
        "/http-runtime",
        "/http-runtime",
        "/http-runtime?status=503",
        "/http-runtime?status=503",
    ]
    assert list(tmp_path.iterdir()) == []


def test_transport_import_failure_is_reported_by_the_process(tmp_path):
    with _http_runtime_server() as (url, requests):
        result = _run_headless(
            ["--verify-http-runtime", url],
            cwd=tmp_path,
            prelude="sys.modules['requests'] = None; ",
        )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "schema": "solin.http-runtime.v1",
        "ok": False,
        "error": "ModuleNotFoundError",
    }
    assert requests == []


def test_rejected_audit_hook_registration_fails_closed(tmp_path):
    prelude = (
        "exec(\"def reject_hook(event, args):\\n"
        "    if event == 'sys.addaudithook': raise RuntimeError('blocked')\"); "
        "sys.addaudithook(reject_hook); "
    )
    with _http_runtime_server() as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path, prelude=prelude)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["error"] == "RuntimeError"
    assert "could not establish" in result.stderr
    assert requests == []


def test_final_expected_status_cannot_succeed_after_deadline(monkeypatch):
    from solin.bootstrap import runtime_verification
    from solin.core.network import http

    clock = iter([0.0, *([0.0] * 10), 0.0, 0.0, 0.0, 11.0])
    monkeypatch.setattr(runtime_verification, "monotonic", lambda: next(clock))

    class Stream:
        status_code = 200
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return

        def iter_bytes(self, chunk_size):
            payload = runtime_verification.HTTP_RUNTIME_PAYLOAD
            for index in range(0, len(payload), chunk_size):
                yield payload[index:index + chunk_size]

    class Transport:
        def get(self, request):
            if "?" in request.url:
                raise http.HttpStatusError(request.url, 503, "expected")
            return http.HttpResponse(
                request.url, 200, http.HttpHeaders({"Content-Encoding": "gzip"}),
                runtime_verification.HTTP_RUNTIME_PAYLOAD,
            )

        def stream(self, request):
            if "?" in request.url:
                raise http.HttpStatusError(request.url, 503, "expected")
            return Stream()

    monkeypatch.setattr(http, "RequestsHttpTransport", Transport)
    with pytest.raises(TimeoutError, match="exceeded its deadline"):
        runtime_verification._verify("http://127.0.0.1:123/runtime")


def test_wrong_payload_fails_qualification(tmp_path):
    class WrongPayload(_HttpRuntimeHandler):
        payload = b'{"unexpected":true}'

    with _http_runtime_server(WrongPayload) as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "RuntimeError"
    assert requests == ["/http-runtime"]


@pytest.mark.parametrize(
    "location",
    [
        "http://example.test:80/forbidden",
        "http://192.0.2.1:80/forbidden",
    ],
)
def test_redirect_cannot_escape_literal_endpoint(tmp_path, location):
    class Redirect(_HttpRuntimeHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _http_runtime_server(Redirect) as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
    assert requests == ["/http-runtime"]
    assert "qualification cannot" in result.stderr


def test_http_status_error_must_have_the_expected_status(tmp_path):
    class WrongStatus(_HttpRuntimeHandler):
        def do_GET(self):
            if "?" not in self.path:
                super().do_GET()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _http_runtime_server(WrongStatus) as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "RuntimeError"
    assert len(requests) == 3


def test_stream_payload_is_verified_independently_and_closed(tmp_path):
    calls = 0

    class WrongStream(_HttpRuntimeHandler):
        def do_GET(self):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.payload = b'{"unexpected":"stream"}'
            super().do_GET()

    with _http_runtime_server(WrongStream) as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "RuntimeError"
    assert requests == ["/http-runtime", "/http-runtime"]


@pytest.mark.parametrize("accepted_call", [3, 4])
def test_get_and_stream_must_reject_unsuccessful_status(tmp_path, accepted_call):
    calls = 0

    class AcceptedStatus(_HttpRuntimeHandler):
        def do_GET(self):
            nonlocal calls
            calls += 1
            if calls == accepted_call:
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_GET()

    with _http_runtime_server(AcceptedStatus) as (url, requests):
        result = _run_headless(["--verify-http-runtime", url], cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "RuntimeError"
    assert len(requests) == accepted_call


def test_external_helper_does_not_qualify_host_python():
    with pytest.raises(AssertionError, match="Packaged HTTP qualification exited"):
        assert_packaged_http_runtime(Path(sys.executable))


def test_external_helper_launches_supplied_path_and_requires_actual_requests(tmp_path, monkeypatch):
    # Simulate a launcher forwarding to the source entrypoint. This verifies the
    # helper's process contract; artifact-gated E2E still runs only the supplied executable.
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen
    launches = []

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        code = (
            "import runpy, sys; "
            "sys.modules['PySide6'] = None; "
            "sys.modules['solin.bootstrap.application'] = None; "
            f"runpy.run_path({str(REPO_ROOT / 'main.py')!r}, run_name='__main__')"
        )
        return original_popen([sys.executable, "-c", code, *command[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "untrusted-host-packages"
    assert_packaged_http_runtime(executable, env=environment)
    command, options = launches[0]
    assert command[:2] == [str(executable.resolve()), "--verify-http-runtime"]
    assert options["cwd"] == executable.parent
    assert "PYTHONPATH" not in options["env"]
    assert environment["PYTHONPATH"] == "untrusted-host-packages"


def test_external_helper_rejects_a_success_report_without_http_requests(tmp_path, monkeypatch):
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen
    report = (
        '{"schema":"solin.http-runtime.v1","ok":true,'
        '"checks":["get","stream","get_status","stream_status"]}'
    )

    def launch(command, **kwargs):
        return original_popen([sys.executable, "-c", f"print({report!r})"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(AssertionError, match="did not exercise all HTTP operations"):
        assert_packaged_http_runtime(executable)


def test_external_helper_timeout_terminates_and_reaps_owned_process(tmp_path, monkeypatch):
    executable = tmp_path / "artifact"
    executable.touch()
    original_popen = subprocess.Popen
    processes = []

    def launch(command, **kwargs):
        process = original_popen(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(pytest.fail.Exception, match="exceeded"):
        assert_packaged_http_runtime(executable, timeout=0.2)
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed
    assert processes[0].stderr.closed
