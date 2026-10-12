"""Factory for the supervised libobs scene engine.

The sidecar uses the shared process supervisor for launch, heartbeat, restart
and IPC on every supported platform.
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


def create_libobs_scene_engine(images_dir: Path | None = None) -> SubprocessSceneEngine:
    """Build a supervised libobs scene engine.

    Source runs launch ``<python> -m solin.core.scenes.libobs_sidecar``; compiled
    runs launch the application executable in its dedicated sidecar role. Both use
    the shared ``SubprocessSceneEngine`` supervisor; libobs is provided by pylibobs.

    ``images_dir`` (the profile's scene images directory) is exported so the
    sidecar can resolve image scene sources' asset ids to files; it is passed
    through the inherited process environment (the supervisor copies os.environ
    when it launches the sidecar).
    """
    if images_dir is not None:
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
