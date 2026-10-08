"""Headless qualification of the delivered HTTP adapter, without application state."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from collections.abc import Sequence
from time import monotonic
from urllib.parse import urlsplit


HTTP_RUNTIME_PAYLOAD = b'{"capability":"solin.http-runtime.v1","message":"gzip streaming OK"}\n'
HTTP_RUNTIME_CHECKS = ("get", "stream", "get_status", "stream_status")
_SCHEMA = "solin.http-runtime.v1"
_REQUEST_TIMEOUT = (2.0, 2.0)
_VERIFICATION_SECONDS = 10.0


def validate_loopback_url(url: str) -> tuple[str, int]:
    """Accept only literal loopback HTTP endpoints with an explicit port."""
    if not url or any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("Expected a literal loopback HTTP URL.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or "%" in parsed.hostname
    ):
        raise ValueError(
            "Expected a literal loopback HTTP URL without credentials, query or fragment."
        )
    address = ipaddress.ip_address(parsed.hostname)
    port = parsed.port
    if not address.is_loopback or port is None or port == 0:
        raise ValueError("Expected a loopback address and an explicit nonzero port.")
    return str(address), port


def _confine_network(address: str, port: int) -> None:
    # This process role exits after qualification. Never read operator netrc or
    # proxy settings, and deny redirects before DNS resolution or socket connect.
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            del os.environ[name]
    os.environ["NO_PROXY"] = "*"
    os.environ["NETRC"] = os.devnull

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "socket.getaddrinfo":
            host, target_port = arguments[:2]
        elif event == "socket.connect":
            destination = arguments[1]
            if not isinstance(destination, tuple) or len(destination) < 2:
                raise PermissionError("HTTP qualification cannot connect outside its endpoint.")
            host, target_port = destination[:2]
        else:
            return
        try:
            target_address = str(ipaddress.ip_address(str(host)))
        except ValueError as error:
            raise PermissionError("HTTP qualification cannot resolve hostnames.") from error
        if target_address != address or target_port != port:
            raise PermissionError("HTTP qualification cannot connect outside its endpoint.")

    sys.addaudithook(audit)
    # CPython suppresses RuntimeError raised by an existing hook while another
    # hook is registered. Verify the fence without resolving or connecting.
    try:
        sys.audit("socket.getaddrinfo", "qualification.invalid", port, 0, 0, 0)
    except PermissionError:
        return
    raise RuntimeError("HTTP qualification could not establish its network fence.")


def _verify(url: str) -> None:
    from solin.core.network.http import HttpRequest, HttpStatusError, RequestsHttpTransport

    deadline = monotonic() + _VERIFICATION_SECONDS
    transport = RequestsHttpTransport()
    request = HttpRequest(url, timeout=_REQUEST_TIMEOUT, headers={"Accept-Encoding": "gzip"})
    response = transport.get(request)
    if (
        response.status_code != 200
        or response.headers.get("Content-Encoding") != "gzip"
        or response.content != HTTP_RUNTIME_PAYLOAD
        or response.json() != json.loads(HTTP_RUNTIME_PAYLOAD)
    ):
        raise RuntimeError("GET did not return the qualification gzip JSON payload.")

    with transport.stream(request) as stream:
        if stream.status_code != 200 or stream.headers.get("Content-Encoding") != "gzip":
            raise RuntimeError("Streaming did not return HTTP 200 with gzip encoding.")
        body = bytearray()
        for chunk in stream.iter_bytes(chunk_size=7):
            if monotonic() >= deadline:
                raise TimeoutError("HTTP qualification exceeded its deadline.")
            body.extend(chunk)
            if len(body) > len(HTTP_RUNTIME_PAYLOAD):
                raise RuntimeError("Streaming exceeded the qualification payload size.")
        if body != HTTP_RUNTIME_PAYLOAD:
            raise RuntimeError("Streaming did not return the qualification payload.")

    failure_request = HttpRequest(f"{url}?status=503", timeout=_REQUEST_TIMEOUT)
    for operation in (transport.get, transport.stream):
        if monotonic() >= deadline:
            raise TimeoutError("HTTP qualification exceeded its deadline.")
        try:
            unexpected = operation(failure_request)
        except HttpStatusError as error:
            if error.status_code != 503:
                raise RuntimeError("Status handling did not report HTTP 503.") from error
        else:
            close = getattr(unexpected, "close", None)
            if close is not None:
                close()
            raise RuntimeError("The transport accepted HTTP 503.")
        if monotonic() >= deadline:
            raise TimeoutError("HTTP qualification exceeded its deadline.")


def main(arguments: Sequence[str]) -> int:
    """Run the isolated process role and emit one deterministic JSON result."""
    result: dict[str, object] = {"schema": _SCHEMA, "ok": False}
    exit_code = 1
    try:
        if len(arguments) != 2 or arguments[0] != "--verify-http-runtime":
            raise ValueError("Usage: --verify-http-runtime <loopback HTTP URL>")
        url = arguments[1]
        address, port = validate_loopback_url(url)
    except ValueError as error:
        result["error"] = type(error).__name__
        print(str(error), file=sys.stderr, flush=True)
        exit_code = 2
    else:
        try:
            _confine_network(address, port)
            _verify(url)
        except Exception as error:  # noqa: BLE001 - qualification process boundary includes import failures
            result["error"] = type(error).__name__
            print(str(error), file=sys.stderr, flush=True)
        else:
            result.update(ok=True, checks=HTTP_RUNTIME_CHECKS)
            exit_code = 0
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return exit_code
