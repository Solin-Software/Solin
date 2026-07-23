from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True, slots=True)
class MainWindowServiceFactories:
    auto_key_dispatcher: Callable[[Any, QObject], Any]
    obs_websocket: Callable[[Any, QObject], Any]
    ndi_receiver: Callable[[QObject], Any]
    camera: Callable[[QObject], Any]
    zoom: Callable[[Any, QObject], Any]
    background_song: Callable[[Any, Any, Any, Any, Any, QObject], Any]
    yeartext: Callable[[QObject], Any]
    jwpub: Callable[[QObject], Any]
    memorial: Callable[[QObject], Any]
    remote_services: Callable[[QWidget], Any]
    auto_share_workers: Callable[[], Any]
    application_maintenance: Callable[[], None]
