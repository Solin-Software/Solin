"""Factory + selection for the libobs scene engine (a supervised sidecar).

The libobs engine reuses main's :class:`~solin.core.scenes.process_engine.SubprocessSceneEngine`
supervision (launch, heartbeat, restart, IPC) but points it at the libobs sidecar
(:mod:`solin.core.scenes.libobs_sidecar`) instead of the native GStreamer
executable. It is selected at bootstrap when ``SOLIN_SCENE_ENGINE=libobs``, so it
never changes default behaviour until explicitly opted into.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from solin.core.scenes.process_engine import (
    SceneEngineProcessConfig,
    SubprocessSceneEngine,
)

_SIDECAR_MODULE = "solin.core.scenes.libobs_sidecar"
ENGINE_SELECTION_ENV = "SOLIN_SCENE_ENGINE"
_LIBOBS_SELECTION_VALUE = "libobs"


def libobs_scene_engine_selected() -> bool:
    """True when the operator opted into the libobs engine via the environment."""
    selection = os.environ.get(ENGINE_SELECTION_ENV, "").strip().lower()
    return selection == _LIBOBS_SELECTION_VALUE


def create_libobs_scene_engine(images_dir: Path | None = None) -> SubprocessSceneEngine:
    """Build a supervised libobs scene engine.

    Launches ``<python> -m solin.core.scenes.libobs_sidecar`` under the same
    ``SubprocessSceneEngine`` client that drives the native engine, so all of the
    supervision / heartbeat / restart / protocol machinery is shared. No
    GStreamer runtime is configured (libobs is self-contained via pylibobs).

    ``images_dir`` (the profile's scene images directory) is exported so the
    sidecar can resolve image scene sources' asset ids to files; it is passed
    through the inherited process environment (the supervisor copies os.environ
    when it launches the sidecar).
    """
    if images_dir is not None:
        os.environ["SOLIN_SCENE_IMAGES_DIR"] = str(images_dir)
        # The app renders the styled year text to this PNG; the sidecar shows it as
        # the "Year text" scene source. Exported before the content controller is
        # built so the file exists by the first hydrate.
        os.environ["SOLIN_YEARTEXT_IMAGE"] = str(images_dir / "__solin_yeartext__.png")
    return SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=Path(sys.executable),
            arguments=("-m", _SIDECAR_MODULE),
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
