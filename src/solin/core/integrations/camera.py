"""Camera discovery for the libobs media pipeline.

Discovery asks **libobs** which cameras it can open (``camera_source.libobs_cameras``)
rather than Qt. That is not a stylistic choice: on Windows Qt enumerates through
Media Foundation while libobs opens cameras through DirectShow, so a Qt device id
handed to ``dshow_input`` fails to decode and the camera renders black. Asking the
component that will actually open the device keeps the ids correct by construction.

Capture itself is libobs' too — the projector opens the camera as a source and the
preview comes off the program tap (see ``live_integration_controller``). This
module therefore no longer captures anything; it discovers devices and tracks
which one is selected.

``CameraService`` keeps its signal-based API so callers are unaffected.
"""

from __future__ import annotations

import logging
import platform
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from solin.core.integrations import camera_options


_log = logging.getLogger(__name__)


# ── Platform helpers ──────────────────────────────────────────────────────────

IS_WINDOWS = platform.system().lower() == "windows"
IS_MACOS   = platform.system().lower() == "darwin"
IS_LINUX   = not IS_WINDOWS and not IS_MACOS


# ── Service ───────────────────────────────────────────────────────────────────

class CameraService(QObject):
    """
    Async camera service.  All state changes are communicated via Qt signals.

    Signals
    ───────
    frame_ready    QImage   New frame available.
    started        str      Name of the successfully started camera.
    stopped        —        Capture ended (after stop() or unrecoverable error).
    error          str      Unrecoverable error message.
    status_changed str      Informational / diagnostic update.
    cameras_ready  list     Updated list[CameraOption] after refresh_cameras().
    """

    frame_ready    = Signal(QImage)
    started        = Signal(str)
    stopped        = Signal()
    error          = Signal(str)
    status_changed = Signal(str)
    cameras_ready  = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._active: Optional[camera_options.CameraOption] = None
        self._known: Optional[list[camera_options.CameraOption]] = None


    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._active is not None

    @property
    def active_camera(self) -> Optional[camera_options.CameraOption]:
        return self._active

    # ── Discovery ─────────────────────────────────────────────────────────────

    def known_cameras(self) -> list[camera_options.CameraOption]:
        return list(self._known) if self._known is not None else []

    def refresh_cameras(self) -> list[camera_options.CameraOption]:
        self._known = discover_cameras()
        self.cameras_ready.emit(self.known_cameras())
        return self.known_cameras()

    def find_saved(
        self,
        backend: str,
        name: str,
    ) -> Optional[camera_options.CameraOption]:
        """
        Locate a previously saved camera by backend + name.

        Applies legacy backend aliases so that preferences saved with old backend
        names ("cv2", "cv2_dshow", "dshow") still resolve correctly.
        Falls back to name-only match if the backend differs.
        """
        backend = camera_options.normalize_camera_backend(backend)
        name    = (name or "").strip()

        if self._known is None:
            self.refresh_cameras()

        known = self._known or []

        for opt in known:
            if backend and opt.backend.value == backend and opt.name == name:
                return opt
        for opt in known:
            if opt.name == name:
                return opt

        return None

    # ── Capture control ───────────────────────────────────────────────────────

    def start(self, option: camera_options.CameraOption) -> None:
        """Mark ``option`` as the active camera.

        Opening the device is libobs' job — the projector creates the capture
        source and the preview is tapped off the program. This only records the
        selection and reports it, so existing signal consumers keep working.
        """
        _log.info(
            "CameraService.start: name=%r backend=%s device_path=%r",
            option.name, option.backend.value, option.device_path,
        )
        self.stop()
        self._active = option
        self.status_changed.emit(f"Selected — '{option.name}'")
        self.started.emit(option.name)

    def stop(self) -> None:
        """Clear the active camera. Emits ``stopped()`` if one was selected."""
        was_running = self._active is not None
        self._active = None
        if was_running:
            self.stopped.emit()


# ── Camera discovery ──────────────────────────────────────────────────────────



def discover_cameras() -> list[camera_options.CameraOption]:
    """Every camera libobs can open, in the order it reports them.

    Enumerated through the same capture source that will open the device, so the
    ``device_path`` on each option is guaranteed to be one libobs accepts.
    """
    found: list[camera_options.CameraOption] = []
    seen_keys: set[str] = set()

    from ..media.camera_source import libobs_cameras

    try:
        cameras = libobs_cameras()
    except Exception:  # noqa: BLE001 - enumeration must never break the UI
        _log.warning("Camera enumeration failed", exc_info=True)
        cameras = []

    for index, (name, device_id) in enumerate(cameras):
        if not name.strip():
            _log.debug("discover: skipping blank-name device")
            continue
        option = camera_options.CameraOption(
            name=name,
            label=name,
            backend=camera_options.CameraBackend.QT,
            cv_index=index,
            device_path=device_id,
        )
        if option.key in seen_keys:
            _log.debug("discover: duplicate key %r, skipping", option.key)
            continue
        seen_keys.add(option.key)
        found.append(option)

    _log.info(
        "discover_cameras() → %d camera(s): %s", len(found), [o.name for o in found]
    )
    return found
