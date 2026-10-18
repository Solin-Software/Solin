from __future__ import annotations

from urllib.parse import urlsplit

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_safe_remote_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if not hostname:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and hostname in _LOCAL_HOSTS)
