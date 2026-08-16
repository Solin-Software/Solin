"""
Camera capture service — Qt Multimedia backend.

Architecture
────────────
Discovery   → QMediaDevices  (all platforms — physical + system-registered cameras)

Capture     → QCamera + QMediaCaptureSession + QVideoSink   (main thread)
                • Qt manages AVFoundation / V4L2 internally with the correct
                  native event-loop integration.
                • QVideoSink.videoFrameChanged → QImage → consumer via frame_ready.
                • All Qt multimedia objects live on the main thread.

Public API  → CameraService (QObject, all interaction via Qt signals)

Aspect ratio
────────────
The emitted QImage carries exactly the dimensions the source delivers. No
scaling is applied inside this service. If the display widget distorts the
image, set Qt.KeepAspectRatio on the widget — that is a display concern.

Migration note (backend name change)
─────────────────────────────────────
The old CameraBackend.CV2 ("cv2") and CameraBackend.CV2_DSHOW ("cv2_dshow")
have been removed. CameraService.find_saved() automatically maps these to
CameraBackend.QT ("qt") so persisted preferences continue to work.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import (
    QCamera,
    QCameraDevice,
    QMediaCaptureSession,
    QMediaDevices,
    QVideoFrame,
    QVideoSink,
)

from solin.core.foundation.constants import APP_PLATFORM
from solin.core.integrations import camera_options


_log = logging.getLogger(__name__)


class CameraService(QObject):
    """
    Async camera service. All state changes are communicated via Qt signals.

    Signals
    ───────
    frame_ready    QImage   New frame available.
    started        str      Name of the successfully started camera.
    stopped        —        Capture ended (after stop() or unrecoverable error).
    error          str      Unrecoverable error message.
    status_changed str      Informational / diagnostic update.
    cameras_ready  list     Updated list[CameraOption] after refresh_cameras().
    """

    frame_ready = Signal(QImage)
    started = Signal(str)
    stopped = Signal()
    error = Signal(str)
    status_changed = Signal(str)
    cameras_ready = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._active: Optional[camera_options.CameraOption] = None
        self._known: Optional[list[camera_options.CameraOption]] = None

        # Qt multimedia objects live on the main thread — no worker thread.
        self._qt_camera: Optional[QCamera] = None
        self._qt_session: QMediaCaptureSession = QMediaCaptureSession(self)
        self._qt_sink: QVideoSink = QVideoSink(self)
        self._qt_session.setVideoSink(self._qt_sink)
        self._qt_sink.videoFrameChanged.connect(self._on_qt_frame)

    @property
    def is_running(self) -> bool:
        return self._active is not None

    @property
    def active_camera(self) -> Optional[camera_options.CameraOption]:
        return self._active

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
        name = (name or "").strip()

        if self._known is None:
            self.refresh_cameras()

        known = self._known or []

        for option in known:
            if backend and option.backend.value == backend and option.name == name:
                return option
        for option in known:
            if option.name == name:
                return option

        return None

    def start(self, option: camera_options.CameraOption) -> None:
        """Start capturing from the given camera. Stops any active capture first."""
        _log.info(
            "CameraService.start: name=%r backend=%s index=%d device_path=%r",
            option.name,
            option.backend.value,
            option.cv_index,
            option.device_path,
        )
        self.stop()
        self._active = option

        qt_device = _find_qt_device(option)
        if qt_device is None:
            message = (
                f"Cannot locate QCameraDevice for '{option.name}' "
                f"(index={option.cv_index}, path={option.device_path!r}). "
                "Is the camera still connected?"
            )
            _log.error(message)
            self._active = None
            self.error.emit(message)
            return

        self._start_qt(option, qt_device)

    def stop(self) -> None:
        """Stop the active capture. Emits stopped() if something was running."""
        if self._qt_camera is not None:
            try:
                self._qt_camera.stop()
                self._qt_session.setCamera(None)
                self._qt_camera.deleteLater()
            except Exception:  # noqa: BLE001 - Qt multimedia cleanup boundary
                _log.debug("Failed to release Qt camera cleanly", exc_info=True)
            self._qt_camera = None

        was_running = self._active is not None
        self._active = None

        if was_running:
            self.stopped.emit()

    def _start_qt(
        self,
        option: camera_options.CameraOption,
        qt_device: QCameraDevice,
    ) -> None:
        """Start a physical camera via Qt's media stack on the main thread."""
        try:
            camera = QCamera(qt_device, self)
            camera.errorOccurred.connect(self._on_qt_error)
            self._qt_session.setCamera(camera)
            self._qt_camera = camera
            camera.start()

            camera_format = camera.cameraFormat()
            resolution = camera_format.resolution()
            backend_label = "AVFoundation" if APP_PLATFORM == "macos" else "V4L2"
            _log.info(
                "[%s] QCamera started — %d×%d @ %.2f fps pixel_format=%s",
                option.name,
                resolution.width(),
                resolution.height(),
                camera_format.maxFrameRate(),
                camera_format.pixelFormat(),
            )
            self.status_changed.emit(
                f"Opened — Qt/{backend_label} | "
                f"{resolution.width()}×{resolution.height()} @ "
                f"{camera_format.maxFrameRate():.0f} fps | "
                f"'{option.name}'"
            )
            self.started.emit(option.name)
        except Exception as exc:  # noqa: BLE001 - Qt multimedia startup boundary
            _log.exception("[%s] Failed to start QCamera", option.name)
            self._active = None
            self.error.emit(str(exc))

    @Slot(QVideoFrame)
    def _on_qt_frame(self, frame: QVideoFrame) -> None:
        if self._active is None or not frame.isValid():
            return
        try:
            image = frame.toImage()
        except Exception:  # noqa: BLE001 - Qt video-frame conversion boundary
            image = QImage()
        if not image.isNull():
            if image.format() != QImage.Format.Format_RGB888:
                image = image.convertToFormat(QImage.Format.Format_RGB888)
            self.frame_ready.emit(image.copy())

    def _on_qt_error(self, *_args: object) -> None:
        if self._qt_camera is None:
            return
        message = self._qt_camera.errorString() or "Camera capture failed."
        _log.error("[Qt] Camera error: %s", message)
        self.stop()
        self.error.emit(message)


def _extract_device_path(qt_device: QCameraDevice) -> str:
    """
    Decode QCameraDevice.id() into a plain Python string.

    Platform-specific content:
      macOS — AVFoundation UID.
      Linux — /dev/videoN path.

    Returns empty string on failure.
    """
    try:
        raw: bytes = bytes(qt_device.id())
        path = raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
        _log.debug(
            "_extract_device_path %r: raw=%r → %r",
            qt_device.description(),
            raw,
            path,
        )
        return path
    except Exception as exc:  # noqa: BLE001 - Qt camera-device metadata boundary
        _log.warning(
            "_extract_device_path failed for %r: %s",
            qt_device.description(),
            exc,
        )
        return ""


def _find_qt_device(
    option: camera_options.CameraOption,
) -> Optional[QCameraDevice]:
    """
    Find the live QCameraDevice corresponding to a CameraOption.

    Search order:
      1. device_path match (stable across reboots and re-enumerations)
      2. description match (reliable if no path; may fail if names clash)
      3. index fallback (fragile if device order changed)

    Returns None if the camera is no longer present.
    """
    devices = list(QMediaDevices.videoInputs())

    if option.device_path:
        for device in devices:
            if _extract_device_path(device) == option.device_path:
                return device

    for device in devices:
        if device.description() == option.name:
            return device

    if 0 <= option.cv_index < len(devices):
        _log.warning(
            "_find_qt_device: falling back to index %d for %r",
            option.cv_index,
            option.name,
        )
        return devices[option.cv_index]

    return None


def discover_cameras() -> list[camera_options.CameraOption]:
    """
    Build the list of available cameras via QMediaDevices.

    Returns CameraOption instances with CameraBackend.QT for every camera
    reported by the OS media stack (AVFoundation on macOS, V4L2 on Linux).
    """
    found: list[camera_options.CameraOption] = []
    seen_keys: set[str] = set()

    def _add(option: camera_options.CameraOption) -> None:
        if not option.name.strip():
            _log.debug("discover: skipping blank-name device")
            return
        if option.key in seen_keys:
            _log.debug("discover: duplicate key %r, skipping", option.key)
            return
        seen_keys.add(option.key)
        found.append(option)
        _log.debug(
            "discover: added %r (backend=%s index=%d path=%r)",
            option.name,
            option.backend.value,
            option.cv_index,
            option.device_path,
        )

    try:
        qt_devices = list(QMediaDevices.videoInputs())
        _log.info("QMediaDevices.videoInputs() → %d device(s)", len(qt_devices))
    except Exception as exc:  # noqa: BLE001 - Qt camera enumeration boundary
        _log.warning("QMediaDevices.videoInputs() failed: %s", exc)
        qt_devices = []

    for index, device in enumerate(qt_devices):
        _add(
            camera_options.CameraOption(
                name=device.description(),
                label=device.description(),
                backend=camera_options.CameraBackend.QT,
                cv_index=index,
                device_path=_extract_device_path(device),
            )
        )

    _log.info(
        "discover_cameras() → %d camera(s): %s",
        len(found),
        [option.name for option in found],
    )
    return found
