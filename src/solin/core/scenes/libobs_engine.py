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


def create_libobs_scene_engine() -> SubprocessSceneEngine:
    """Build a supervised libobs scene engine.

    Launches ``<python> -m solin.core.scenes.libobs_sidecar`` under the same
    ``SubprocessSceneEngine`` client that drives the native engine, so all of the
    supervision / heartbeat / restart / protocol machinery is shared. No
    GStreamer runtime is configured (libobs is self-contained via pylibobs).
    """
    return SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=Path(sys.executable),
            arguments=("-m", _SIDECAR_MODULE),
        )
    )
