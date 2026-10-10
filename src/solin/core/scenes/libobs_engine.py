"""Factory + selection for the libobs scene engine (a supervised sidecar).

The libobs engine reuses main's :class:`~solin.core.scenes.process_engine.SubprocessSceneEngine`
supervision (launch, heartbeat, restart, IPC) but points it at the libobs sidecar
(:mod:`solin.core.scenes.libobs_sidecar`) instead of the native GStreamer
executable. On this branch libobs is THE engine, so it is selected by default;
``SOLIN_SCENE_ENGINE`` only exists as an escape hatch to opt back out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from solin.core.foundation.constants import IS_DEV, LIBOBS_SIDECAR_ARGUMENT
from solin.core.scenes.process_engine import (
    SceneEngineProcessConfig,
    SubprocessSceneEngine,
)

_SIDECAR_MODULE = "solin.core.scenes.libobs_sidecar"
ENGINE_SELECTION_ENV = "SOLIN_SCENE_ENGINE"
_LIBOBS_SELECTION_VALUE = "libobs"


def libobs_scene_engine_selected() -> bool:
    """True unless the operator explicitly asked for a different engine.

    libobs is the only media engine on this branch, so it is the default and
    needs no environment variable. ``SOLIN_SCENE_ENGINE`` remains an escape
    hatch: set it to anything other than ``libobs`` (e.g. ``native``) to opt out.
    """
    selection = os.environ.get(ENGINE_SELECTION_ENV, "").strip().lower()
    return not selection or selection == _LIBOBS_SELECTION_VALUE


def create_libobs_scene_engine(images_dir: Path | None = None) -> SubprocessSceneEngine:
    """Build a supervised libobs scene engine.

    Source runs launch ``<python> -m solin.core.scenes.libobs_sidecar``; compiled
    runs launch the application executable in its dedicated sidecar role. Both use
    ``SubprocessSceneEngine`` client that drives the native engine, so all of the
    supervision / heartbeat / restart / protocol machinery is shared. No
    GStreamer runtime is configured (libobs is self-contained via pylibobs).

    ``images_dir`` (the profile's scene images directory) is exported so the
    sidecar can resolve image scene sources' asset ids to files; it is passed
    through the inherited process environment (the supervisor copies os.environ
    when it launches the sidecar).
    """
    if images_dir is not None:
        # Callers may hand this over as a plain string; libobs is the default
        # engine now, so this runs on every start and must not care.
        images_dir = Path(images_dir)
        os.environ["SOLIN_SCENE_IMAGES_DIR"] = str(images_dir)
    return SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=Path(sys.executable),
            arguments=("-m", _SIDECAR_MODULE) if IS_DEV else (LIBOBS_SIDECAR_ARGUMENT,),
            # Resilience: the sidecar handles IPC (incl. heartbeats) on one thread,
            # so a heavy hydrate under load — many outputs + a flaky camera
            # saturating the single obs graphics thread — can briefly delay a
            # heartbeat. A tight timeout would kill a *working* engine and, after a
            # few such kills, the supervisor would give up mid-meeting. Give the
            # heartbeat generous slack and allow many restarts over a long window so
            # a transiently-slow (not dead) engine is never abandoned; the app
            # re-hydrates automatically on each restart.
            heartbeat_interval_ms=1000,
            heartbeat_timeout_ms=10000,
            maximum_restarts=30,
            restart_window_seconds=180.0,
        )
    )
