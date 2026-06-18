from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject


@dataclass(frozen=True, slots=True)
class MainWindowServiceFactories:
    auto_key_dispatcher: Callable[[Any, QObject], Any]
    obs_websocket: Callable[[Any, QObject], Any]
    ndi_receiver: Callable[[QObject], Any]
    camera: Callable[[QObject], Any]
    zoom: Callable[[Any, QObject], Any]
    background_song: Callable[[Any, Any, Any, Any, Any, QObject], Any]
