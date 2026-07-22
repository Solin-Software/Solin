"""Source-bound identities for persistent media thumbnails."""

from __future__ import annotations

import hashlib
import os


def thumbnail_source_fingerprint(source: str) -> str:
    if not source:
        return ""
    identity = source
    if not source.startswith(("http://", "https://")):
        identity = os.path.normcase(os.path.normpath(os.path.abspath(source)))
    return hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()


def thumbnail_storage_id(base_id: str, source: str) -> str:
    if not base_id:
        return ""
    fingerprint = thumbnail_source_fingerprint(source)
    return f"{base_id}-{fingerprint[:16]}" if fingerprint else base_id


__all__ = ["thumbnail_source_fingerprint", "thumbnail_storage_id"]
