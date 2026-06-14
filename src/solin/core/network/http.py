"""Transport-neutral HTTP primitives shared by Solin services."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import certifi
import requests

DEFAULT_TIMEOUT = 15.0
DEFAULT_CHUNK_SIZE = 131_072

HttpParamValue = str | int | float | bool | None
HttpTimeout = float | tuple[float, float]


class HttpError(RuntimeError):
    """Base class for transport-independent HTTP failures."""


class HttpTransportError(HttpError):
    """The request could not be completed due to connectivity or TLS failure."""


class HttpTimeoutError(HttpTransportError):
    """The request did not finish before its configured timeout."""


class HttpStatusError(HttpError):
    """The server responded with an unsuccessful HTTP status."""

    def __init__(self, url: str, status_code: int, message: str) -> None:
        super().__init__(f"GET {url} returned HTTP {status_code}: {message}")
        self.url = url
        self.status_code = status_code


class HttpDecodeError(HttpError):
    """The response body could not be decoded as the expected format."""


class HttpResponseTooLargeError(HttpError):
    """The response exceeded the caller's configured byte limit."""

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"GET {url} exceeded the {limit} byte response limit")
        self.url = url
        self.limit = limit


class HttpHeaders(Mapping[str, str]):
    """Case-insensitive response headers with Mapping semantics."""

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        self._items: dict[str, str] = {}
        self._by_lower: dict[str, str] = {}
        for key, value in (headers or {}).items():
            text_key = str(key)
            text_value = str(value)
            self._items[text_key] = text_value
            self._by_lower[text_key.lower()] = text_value

    def __getitem__(self, key: str) -> str:
        return self._by_lower[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str, default: Any = None) -> Any:
        return self._by_lower.get(key.lower(), default)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    timeout: HttpTimeout = DEFAULT_TIMEOUT
    headers: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, HttpParamValue] | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    status_code: int
    headers: HttpHeaders
    content: bytes

    @property
    def content_length(self) -> int:
        value = self.headers.get("Content-Length", "")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def text(self, encoding: str = "utf-8") -> str:
        try:
            return self.content.decode(encoding)
        except UnicodeError as exc:
            raise HttpDecodeError(f"GET {self.url} returned undecodable text: {exc}") from exc

    def json(self, encoding: str = "utf-8") -> Any:
        try:
            return json.loads(self.text(encoding))
        except json.JSONDecodeError as exc:
            raise HttpDecodeError(f"GET {self.url} returned invalid JSON: {exc}") from exc


class HttpByteStream(Protocol):
    url: str
    status_code: int
    headers: HttpHeaders

    @property
    def content_length(self) -> int: ...

    def iter_bytes(self, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]: ...

    def close(self) -> None: ...

    def __enter__(self) -> "HttpByteStream": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class HttpTransport(Protocol):
    """Port implemented by concrete HTTP clients."""

    def get(self, request: HttpRequest) -> HttpResponse: ...

    def stream(self, request: HttpRequest) -> HttpByteStream: ...


class _RequestsByteStream:
    def __init__(self, response: Any, fallback_url: str) -> None:
        self._response = response
        self.url = str(getattr(response, "url", "") or fallback_url)
        self.status_code = int(getattr(response, "status_code", 0) or 0)
        self.headers = HttpHeaders(getattr(response, "headers", {}))

    @property
    def content_length(self) -> int:
        value = self.headers.get("Content-Length", "")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def iter_bytes(self, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        try:
            for chunk in self._response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except requests.Timeout as exc:
            raise HttpTimeoutError(f"GET {self.url} timed out: {exc}") from exc
        except requests.RequestException as exc:
            raise HttpTransportError(f"GET {self.url} failed: {exc}") from exc

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> "_RequestsByteStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class RequestsHttpTransport:
    """Configured production HTTP adapter backed by requests and certifi."""

    def __init__(self, *, verify_tls: str | bool | None = None) -> None:
        self._verify_tls = certifi.where() if verify_tls is None else verify_tls

    def get(self, request: HttpRequest) -> HttpResponse:
        response = self._send(request, stream=False)
        return HttpResponse(
            url=str(getattr(response, "url", "") or request.url),
            status_code=int(getattr(response, "status_code", 0) or 0),
            headers=HttpHeaders(getattr(response, "headers", {})),
            content=bytes(getattr(response, "content", b"")),
        )

    def stream(self, request: HttpRequest) -> HttpByteStream:
        response = self._send(request, stream=True)
        return _RequestsByteStream(response, request.url)

    def _send(self, request: HttpRequest, *, stream: bool) -> Any:
        try:
            response = requests.get(
                request.url,
                headers=dict(request.headers),
                params=request.params,
                timeout=request.timeout,
                stream=stream,
                verify=self._verify_tls,
            )
        except requests.Timeout as exc:
            raise HttpTimeoutError(f"GET {request.url} timed out: {exc}") from exc
        except requests.RequestException as exc:
            raise HttpTransportError(f"GET {request.url} failed: {exc}") from exc

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if stream:
                response.close()
            raise HttpStatusError(
                str(getattr(response, "url", "") or request.url),
                int(getattr(response, "status_code", 0) or 0),
                str(exc),
            ) from exc
        except requests.RequestException as exc:
            if stream:
                response.close()
            raise HttpTransportError(f"GET {request.url} failed: {exc}") from exc

        return response


_DEFAULT_TRANSPORT: HttpTransport = RequestsHttpTransport()


def default_transport() -> HttpTransport:
    return _DEFAULT_TRANSPORT


def configure_default_transport(transport: HttpTransport | None) -> None:
    """Replace the process default transport, primarily for integration tests."""
    global _DEFAULT_TRANSPORT
    _DEFAULT_TRANSPORT = transport or RequestsHttpTransport()


def get(
    url: str,
    *,
    timeout: HttpTimeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, HttpParamValue] | None = None,
    max_bytes: int | None = None,
    transport: HttpTransport | None = None,
) -> HttpResponse:
    request = HttpRequest(
        url=url,
        timeout=timeout,
        headers=headers or {},
        params=params,
    )
    if max_bytes is None:
        return (transport or _DEFAULT_TRANSPORT).get(request)

    with (transport or _DEFAULT_TRANSPORT).stream(request) as response:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise HttpResponseTooLargeError(response.url, max_bytes)
            chunks.append(chunk)
        return HttpResponse(
            url=response.url,
            status_code=response.status_code,
            headers=response.headers,
            content=b"".join(chunks),
        )


def get_bytes(
    url: str,
    *,
    timeout: HttpTimeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, HttpParamValue] | None = None,
    max_bytes: int | None = None,
    transport: HttpTransport | None = None,
) -> bytes:
    return get(
        url,
        timeout=timeout,
        headers=headers,
        params=params,
        max_bytes=max_bytes,
        transport=transport,
    ).content


def get_json(
    url: str,
    *,
    timeout: HttpTimeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, HttpParamValue] | None = None,
    transport: HttpTransport | None = None,
) -> Any:
    return get(
        url,
        timeout=timeout,
        headers=headers,
        params=params,
        transport=transport,
    ).json()


def stream_get(
    url: str,
    *,
    timeout: HttpTimeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, HttpParamValue] | None = None,
    transport: HttpTransport | None = None,
) -> HttpByteStream:
    request = HttpRequest(
        url=url,
        timeout=timeout,
        headers=headers or {},
        params=params,
    )
    return (transport or _DEFAULT_TRANSPORT).stream(request)

