"""Shared presentation of requested scene output and observed delivery health."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.scenes.output_health import OutputHealthStatus


_CONTEXT = "SceneOutputStatus"
_OFF = QT_TRANSLATE_NOOP("SceneOutputStatus", "Off")
_ON = QT_TRANSLATE_NOOP("SceneOutputStatus", "On")
_ERROR = QT_TRANSLATE_NOOP("SceneOutputStatus", "Error")
_STARTING = QT_TRANSLATE_NOOP("SceneOutputStatus", "Starting…")
_VIRTUAL_CAMERA = QT_TRANSLATE_NOOP("SceneOutputStatus", "Virtual camera")
_VIRTUAL_CAMERA_ENABLED = QT_TRANSLATE_NOOP("SceneOutputStatus", "Virtual camera enabled")
_VIRTUAL_CAMERA_DISABLED = QT_TRANSLATE_NOOP("SceneOutputStatus", "Virtual camera disabled")
_VIRTUAL_CAMERA_FAILED = QT_TRANSLATE_NOOP(
    "SceneOutputStatus",
    "The virtual camera failed. Turn it off and on to try again.",
)


def scene_output_state_label(enabled: bool, status: OutputHealthStatus) -> str:
    if not enabled:
        source = _OFF
    elif status is OutputHealthStatus.FAILED:
        source = _ERROR
    elif status is OutputHealthStatus.READY:
        source = _ON
    else:
        source = _STARTING
    return QCoreApplication.translate(_CONTEXT, source)


def virtual_camera_title() -> str:
    return QCoreApplication.translate(_CONTEXT, _VIRTUAL_CAMERA)


def virtual_camera_state_label(enabled: bool, status: OutputHealthStatus) -> str:
    if not enabled:
        return QCoreApplication.translate(_CONTEXT, _VIRTUAL_CAMERA_DISABLED)
    if status is OutputHealthStatus.READY:
        return QCoreApplication.translate(_CONTEXT, _VIRTUAL_CAMERA_ENABLED)
    return f"{virtual_camera_title()} · {scene_output_state_label(enabled, status)}"


def virtual_camera_failure_summary() -> str:
    return QCoreApplication.translate(_CONTEXT, _VIRTUAL_CAMERA_FAILED)
