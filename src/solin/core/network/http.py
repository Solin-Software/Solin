"""HTTP helpers shared by Solin services."""

from __future__ import annotations

import ssl
import urllib.request
from collections.abc import Mapping
from typing import Any

import certifi
import requests

_SSL_CONTEXT: ssl.SSLContext | None = None


class HttpError(RuntimeError):
    """Base class for transport-independent HTTP failures."""


class HttpTransportError(HttpError):
    """The request could not be completed due to connectivity or TLS failure."""


class HttpStatusError(HttpError):
    """The server responded with an unsuccessful HTTP status."""

    def __init__(self, url: str, status_code: int, message: str) -> None:
        super().__init__(f"GET {url} returned HTTP {status_code}: {message}")
        self.url = url
        self.status_code = status_code


class HttpDecodeError(HttpError):
    """The response body could not be decoded as the expected format."""


def ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        cafile = certifi.where()
        _SSL_CONTEXT = ssl.create_default_context(cafile=cafile)
    return _SSL_CONTEXT


def urlopen(url: str | urllib.request.Request, *args: Any, **kwargs: Any):
    """Open URLs using the bundled certifi CA bundle by default."""
    kwargs.setdefault("context", ssl_context())
    return urllib.request.urlopen(url, *args, **kwargs)


def get_json(
    url: str,
    *,
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> Any:
    try:
        response = requests.get(
            url,
            headers=dict(headers or {}),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise HttpTransportError(f"GET {url} failed: {exc}") from exc

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HttpStatusError(
            url,
            int(getattr(response, "status_code", 0) or 0),
            str(exc),
        ) from exc
    except requests.RequestException as exc:
        raise HttpTransportError(f"GET {url} failed: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise HttpDecodeError(f"GET {url} returned invalid JSON: {exc}") from exc

