"""Locate the bundled LiveKit browser client served to attendees.

The attendee page loads ``livekit-client`` (Apache-2.0) and its end-to-end encryption
worker from the hall, never from a CDN, so a meeting works without third-party sites.

Lookup order:
1. ``SOLIN_LIVEKIT_CLIENT`` (a directory, for development and diagnostics).
2. Compiled builds: ``<app dir>/web/livekit-client/``, staged by the build scripts.
3. Development checkouts: ``build/livekit-client/``, fetched by
   ``scripts/fetch_livekit_client.py``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

LIVEKIT_CLIENT_VERSION = "2.23.0"
CLIENT_FILES = ("livekit-client.umd.js", "livekit-client.e2ee.worker.mjs")
OVERRIDE_ENVIRONMENT = "SOLIN_LIVEKIT_CLIENT"
_REPO_ROOT = Path(__file__).resolve().parents[4]


class WebClientUnavailableError(RuntimeError):
    """The browser client files are missing."""


def client_directory(
    *,
    environ: Mapping[str, str] | None = None,
    compiled: bool | None = None,
    app_executable: str | None = None,
    repo_root: Path = _REPO_ROOT,
) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(OVERRIDE_ENVIRONMENT, "").strip()
    if override:
        directory = Path(override)
    else:
        if compiled is None:
            from solin.core.foundation.constants import IS_COMPILED

            compiled = IS_COMPILED
        if compiled:
            directory = Path(app_executable or sys.executable).resolve().parent / "web" / "livekit-client"
        else:
            directory = repo_root / "build" / "livekit-client"
    missing = [name for name in CLIENT_FILES if not (directory / name).is_file()]
    if missing:
        hint = "" if compiled or override else " (run scripts/fetch_livekit_client.py)"
        raise WebClientUnavailableError(f"LiveKit browser client missing in {directory}{hint}")
    return directory
