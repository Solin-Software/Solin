"""Transient OBS connection probe used by onboarding presentation."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from solin.core.integrations.automation.obs import OBSWebSocketService


class _TransientOBSSettings:
    def __init__(self) -> None:
        self._port = 0
        self._password = ""

    def set_connection(self, port: int, password: str) -> None:
        self._port = int(port)
        self._password = password

    def is_configured(self) -> bool:
        return self._port > 0

    def websocket_port(self, default: int = 4455) -> int:
        return self._port if self._port > 0 else default

    def password(self) -> str:
        return self._password


class OnboardingOBSProbe(QObject):
    """Owns the temporary OBS service outside the onboarding widget."""

    state_changed = Signal(object, str)
    scenes_updated = Signal(list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = _TransientOBSSettings()
        self._service = OBSWebSocketService(self._settings, parent=self)
        self._service.state_changed.connect(self.state_changed.emit)
        self._service.scenes_updated.connect(self.scenes_updated.emit)

    @property
    def scenes(self) -> list[str]:
        return self._service.scenes

    def connect_to(self, port: int, password: str) -> None:
        self._settings.set_connection(port, password)
        self._service.stop()
        self._service.start()

    def stop(self) -> None:
        self._service.stop()

    def shutdown(self) -> None:
        self._service.stop(wait=True)
