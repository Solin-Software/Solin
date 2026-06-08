from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlparse

_KNOWN_URL_SCHEMES = {
    "about",
    "blob",
    "chrome",
    "data",
    "devtools",
    "edge",
    "file",
    "ftp",
    "http",
    "https",
    "mailto",
    "view-source",
}


def _local_path_to_file_url(value: str) -> str:
    path = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
    return path.as_uri()


def _repair_file_url(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("file:///"):
        return normalized
    if normalized.startswith("file://"):
        rest = normalized[7:]
        if rest.startswith("/"):
            return "file://" + rest
        return "file:///" + rest.lstrip("/")
    if normalized.startswith("file:/"):
        return "file:///" + normalized[6:].lstrip("/")
    rest = normalized[4:].lstrip(":/")
    return "file:///" + rest


def normalize_browser_input(value: str, *, search_if_text: bool = False) -> str:
    """Normalize address-bar/drop input without corrupting local file URLs."""
    text = (value or "").strip()
    if not text:
        return "about:blank"

    if text.lower().startswith("file"):
        return _repair_file_url(text)

    parsed = urlparse(text)
    if parsed.scheme:
        if parsed.scheme.lower() in _KNOWN_URL_SCHEMES:
            return text
        if len(parsed.scheme) == 1 and os.name == "nt":
            return _local_path_to_file_url(text)
        return text

    expanded = os.path.expandvars(os.path.expanduser(text))
    if os.path.isabs(expanded) or text.startswith(("./", "../", ".\\", "..\\")):
        candidate = Path(expanded)
        if candidate.exists():
            return candidate.resolve().as_uri()

    first = text.split("/", 1)[0]
    looks_like_url = "." in first or first.lower() == "localhost" or ":" in first
    if looks_like_url:
        return "https://" + text

    if search_if_text:
        return "https://www.google.com/search?q=" + quote(text)
    return "https://" + text
