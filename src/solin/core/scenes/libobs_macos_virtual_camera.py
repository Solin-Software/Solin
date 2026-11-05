"""Use the camera sink supplied by an installed OBS camera extension."""
from __future__ import annotations

import logging
from importlib import import_module
from pathlib import Path
import plistlib
import time
import uuid
from typing import Any
from xml.parsers.expat import ExpatError

from solin.core.scenes.output_health import OUTPUT_FRAME_TIMEOUT_S, OutputHealth, OutputHealthStatus

log = logging.getLogger(__name__)


def installed_obs_camera_uid() -> str | None:
    """Read the vendor's device identity without loading its frontend plugin."""
    appkit: Any = import_module("AppKit")
    application = appkit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(
        "com.obsproject.obs-studio")
    if application is None:
        return None
    metadata = Path(application.path()) / "Contents/PlugIns/mac-virtualcam.plugin/Contents/Info.plist"
    try:
        with metadata.open("rb") as stream:
            values = plistlib.load(stream)
        if not isinstance(values, dict):
            raise ValueError("Invalid OBS virtual-camera metadata")
        uid = values["OBSCameraDeviceUUID"]
        if values.get("CFBundleIdentifier") != "com.obsproject.mac-virtualcam" or not isinstance(uid, str):
            raise ValueError("Invalid OBS virtual-camera metadata")
        uuid.UUID(uid)
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException, ExpatError):
        log.warning("Installed OBS does not expose valid camera-extension metadata", exc_info=True)
        return None
    return uid


class LibobsMacVirtualCamera:
    """Own an existing OBS camera's producer; never install another device."""

    def __init__(self, _runtime: Any) -> None:
        self._output: Any = None
        self._last_frame_at = 0.0
        self._total_frames = 0

    @property
    def active(self) -> bool:
        return self._output is not None and self._output.active

    @property
    def health(self) -> OutputHealth:
        output = self._output
        if output is None:
            return OutputHealth()
        if not output.active:
            return OutputHealth(OutputHealthStatus.FAILED, "output_stopped")
        count = output.frames_consumed
        now = time.monotonic()
        if count != self._total_frames:
            self._total_frames = count
            self._last_frame_at = now
        if now - self._last_frame_at >= OUTPUT_FRAME_TIMEOUT_S:
            return OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
        return OutputHealth(OutputHealthStatus.READY if count else OutputHealthStatus.STARTING)

    def start(self) -> bool:
        if self._output is not None:
            return self.active
        uid = installed_obs_camera_uid()
        if uid is None:
            log.warning("macOS virtual camera requires an installed OBS camera extension")
            return False
        from pylibobs.macos_camera import CameraOutput

        try:
            output = CameraOutput(uid)
        except (OSError, RuntimeError, ValueError):
            log.warning("Could not open the installed OBS camera sink; enable its camera extension", exc_info=True)
            return False
        self._output = output
        self._total_frames = 0
        self._last_frame_at = time.monotonic()
        return True

    def stop(self) -> None:
        output, self._output = self._output, None
        if output is not None:
            output.close()
            if output.last_error:
                log.warning("macOS camera output closed with a native error: %s", output.last_error)
