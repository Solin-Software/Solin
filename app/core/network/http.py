"""HTTP helpers shared by Solin services."""

from __future__ import annotations

import ssl
import urllib.request
from typing import Any

import certifi

_SSL_CONTEXT: ssl.SSLContext | None = None


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

