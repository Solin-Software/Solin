"""Profile-scoped remote settings and device onboarding for Qt Quick."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QFileDialog, QWidget

from solin.core.remote_control.network import LanInterface, discover_lan_interfaces
from solin.core.remote_control.security import (
    PASSWORD_MIN_LENGTH,
    PasswordValidationError,
    RemoteControlCredentialsStore,
    UsernameValidationError,
)
from solin.core.remote_control.settings import REMOTE_CONTROL_PORT, RemoteControlSettingsStore
from solin.core.storage.binary_files import write_bytes_atomic
from solin.ui.qr_generation import QrGenerationSession, QrGenerationSessionFactory

from .domain import SettingsDomain


@dataclass(frozen=True, slots=True)
class _Setup:
    access_url: str
    setup_url: str
    verification_code: str
    fingerprint: str
    certificate: bytes

    @property
    def ready(self) -> bool:
        return bool(
            self.access_url.startswith("https://")
            and self.setup_url.startswith("https://")
            and self.verification_code
            and self.fingerprint
            and self.certificate
        )


class RemoteSettings(SettingsDomain):
    """Keep remote runtime state alive independently of instantiated QML pages."""

    remote_control_settings_changed = Signal()
    remote_control_credentials_changed = Signal()

    def __init__(
        self,
        remote_control_settings: RemoteControlSettingsStore,
        remote_control_credentials: RemoteControlCredentialsStore,
        qr_generation_session_factory: QrGenerationSessionFactory,
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = remote_control_settings
        self._credentials = remote_control_credentials
        self._qr_factory = qr_generation_session_factory
        self._dialog_parent = dialog_parent
        self._interfaces: dict[str, LanInterface] = {}
        self._setup: _Setup | None = None
        self._qr_session: QrGenerationSession | None = None
        self._pending_auto_open = False
        self._runtime_known = False
        self._running = False
        self._runtime_message = ""
        self._runtime_kind = "pending"
        self._closed = False
        self.publish(
            username=self._credentials.configured_username(),
            password="",
            passwordConfirmation="",
            setupVisible=False,
            setupEnabled=False,
            setupUrl="",
            accessUrl="",
            verificationCode="",
            fingerprint="",
            fingerprintVisible=False,
            qrImage="",
            qrBusy=False,
            qrError="",
        )
        self.refresh_runtime()

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        if self._closed:
            return
        if key in {"username", "password", "passwordConfirmation"}:
            self.publish(**{key: str(value)}, feedback="", feedbackKind="")
        elif key == "fingerprintVisible":
            self.publish(fingerprintVisible=bool(value))
        elif key == "network":
            interface = self._interfaces.get(str(value))
            if value and interface is None:
                self.fail(QCoreApplication.translate("SettingsWidget", "Select a private network…"))
                return
            if interface is None:
                self._settings.clear_network_selection()
            else:
                self._settings.set_network_selection(interface.identifier, interface.ipv4_address)
            self._invalidate_runtime()
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        elif key == "enabled":
            enabled = bool(value)
            if enabled and not self._configuration_valid():
                self.fail(
                    QCoreApplication.translate(
                        "SettingsWidget",
                        "Choose a network and save credentials before enabling remote control.",
                    )
                )
                return
            self._settings.set_enabled(enabled)
            self._pending_auto_open = enabled and not self._settings.onboarding_seen()
            self._invalidate_runtime()
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        else:
            super().setValue(key, value)

    @Slot(str)
    def invoke(self, action: str) -> None:
        if self._closed:
            return
        actions = {
            "cancelCredentials": self._cancel_credentials,
            "saveCredentials": self._save_credentials,
            "copyAddress": lambda: self._copy(self._state.get("endpoint", "")),
            "openSetup": self._open_setup,
            "closeSetup": self._close_setup,
            "completeSetup": self._complete_setup,
            "copySetupAddress": lambda: self._copy(self._state.get("setupUrl", "")),
            "saveCertificate": self._save_certificate,
        }
        callback = actions.get(action)
        if callback is None:
            super().invoke(action)
            return
        callback()

    def _selection_key(self) -> str:
        selection = self._settings.network_selection()
        return selection.selection_key if selection else ""

    def _configuration_valid(self) -> bool:
        return self._selection_key() in self._interfaces and self._credentials.has_credentials()

    def refresh_runtime(self) -> None:
        if self._closed:
            return
        self._interfaces = {item.selection_key: item for item in discover_lan_interfaces()}
        if not self._configuration_valid():
            self._invalidate_runtime()
        self.publish(
            networkOptions=[
                {
                    "label": QCoreApplication.translate(
                        "SettingsWidget", "Select a private network…"
                    ),
                    "value": "",
                },
                *[
                    {"label": f"{item.display_name}  ·  {item.ipv4_address}", "value": key}
                    for key, item in self._interfaces.items()
                ],
            ]
        )
        self._refresh_status()

    def refresh_language(self) -> None:
        self.refresh_runtime()
        if self._state["qrError"]:
            self.publish(
                qrError=QCoreApplication.translate(
                    "RemoteControlSetupDialog",
                    "Could not generate the QR code. Copy the address instead.",
                )
            )

    def _refresh_status(self) -> None:
        selected = self._selection_key()
        interface = self._interfaces.get(selected)
        configured = self._credentials.has_credentials()
        valid = self._configuration_valid()
        enabled = self._settings.enabled()
        if enabled and valid:
            message = (
                self._runtime_message
                if self._runtime_known
                else QCoreApplication.translate("SettingsWidget", "Starting secure remote control…")
            )
            kind = self._runtime_kind if self._runtime_known else "pending"
        elif valid:
            message, kind = (
                QCoreApplication.translate("SettingsWidget", "Ready to enable."),
                "ready",
            )
        else:
            message, kind = (
                QCoreApplication.translate("SettingsWidget", "Configuration required."),
                "pending",
            )
        self.publish(
            enabled=enabled,
            network=selected if interface else "",
            credentialsConfigured=configured,
            credentialsSummary=(
                QCoreApplication.translate("SettingsWidget", "Configured as %1").replace(
                    "%1", self._credentials.configured_username()
                )
                if configured
                else QCoreApplication.translate("SettingsWidget", "Not configured")
            ),
            passwordHint=QCoreApplication.translate(
                "SettingsWidget",
                "Use at least %1 characters. Credentials belong only to this profile.",
            ).replace("%1", str(PASSWORD_MIN_LENGTH)),
            endpoint=f"https://{interface.ipv4_address}:{REMOTE_CONTROL_PORT}/remote/"
            if interface
            else "",
            statusMessage=message,
            statusKind=kind,
            setupEnabled=bool(enabled and valid and self._running and self._setup),
        )

    def _invalidate_runtime(self) -> None:
        self._runtime_known = False
        self._running = False
        self._runtime_message = ""
        self._runtime_kind = "pending"
        self._setup = None
        self._close_setup()
        self.publish(setupEnabled=False)

    def _cancel_credentials(self) -> None:
        self.publish(
            username=self._credentials.configured_username(),
            password="",
            passwordConfirmation="",
            feedback="",
            feedbackKind="",
        )

    def _save_credentials(self) -> None:
        password = self._state["password"]
        if password != self._state["passwordConfirmation"]:
            self.fail(QCoreApplication.translate("SettingsWidget", "The passwords do not match."))
            return
        if not password:
            self.fail(QCoreApplication.translate("SettingsWidget", "Enter a new password to save."))
            return
        try:
            self._credentials.set_credentials(self._state["username"], password)
        except (UsernameValidationError, PasswordValidationError):
            self.fail(
                QCoreApplication.translate(
                    "SettingsWidget",
                    "Check the username and use a password with at least %1 characters.",
                ).replace("%1", str(PASSWORD_MIN_LENGTH))
            )
            return
        self._cancel_credentials()
        self._invalidate_runtime()
        self._refresh_status()
        self.remote_control_credentials_changed.emit()
        self.succeed(QCoreApplication.translate("SettingsWidget", "Saved"))

    def set_remote_control_runtime_status(
        self,
        *,
        running: bool,
        message: str,
        fingerprint: str = "",
        access_url: str = "",
        setup_url: str = "",
        verification_code: str = "",
        certificate_der: bytes = b"",
        status: str | None = None,
    ) -> None:
        if self._closed:
            return
        presentation = _Setup(
            access_url, setup_url, verification_code, fingerprint, certificate_der
        )
        setup = presentation if running and presentation.ready else None
        if setup != self._setup:
            self._close_setup()
        self._setup = setup
        self._runtime_known = True
        self._running = running
        self._runtime_message = message
        self._runtime_kind = "running" if running else status or "error"
        self._refresh_status()
        if self._state["setupEnabled"] and self._pending_auto_open:
            self._pending_auto_open = False
            self._open_setup()

    def _open_setup(self) -> None:
        if not self._state["setupEnabled"] or self._setup is None or self._state["setupVisible"]:
            return
        setup = self._setup
        octets = setup.fingerprint.split(":")
        fingerprint = (
            "  ".join(":".join(octets[index : index + 4]) for index in range(0, len(octets), 4))
            if len(octets) > 1 and all(len(octet) == 2 for octet in octets)
            else setup.fingerprint
        )
        self.publish(
            setupVisible=True,
            setupUrl=setup.setup_url,
            accessUrl=setup.access_url,
            verificationCode=setup.verification_code,
            fingerprint=fingerprint,
            fingerprintVisible=False,
            qrImage="",
            qrBusy=True,
            qrError="",
        )
        session = self._qr_factory.create(parent=self)
        self._qr_session = session
        session.ready.connect(self._on_qr_ready)
        session.failed.connect(self._on_qr_failed)
        if not session.start(setup.setup_url):
            self._qr_failed(session)

    @Slot(bytes)
    def _on_qr_ready(self, data: bytes) -> None:
        if self.sender() is self._qr_session and self._qr_session is not None:
            self._qr_ready(self._qr_session, data)

    @Slot()
    def _on_qr_failed(self) -> None:
        if self.sender() is self._qr_session and self._qr_session is not None:
            self._qr_failed(self._qr_session)

    def _qr_ready(self, session: QrGenerationSession, data: bytes) -> None:
        if session is not self._qr_session or self._closed:
            return
        if QImage.fromData(data, "PNG").isNull():
            self._qr_failed(session)
            return
        self.publish(
            qrImage="data:image/png;base64," + base64.b64encode(data).decode("ascii"),
            qrBusy=False,
            qrError="",
        )

    def _qr_failed(self, session: QrGenerationSession) -> None:
        if session is self._qr_session and not self._closed:
            self.publish(
                qrBusy=False,
                qrImage="",
                qrError=QCoreApplication.translate(
                    "RemoteControlSetupDialog",
                    "Could not generate the QR code. Copy the address instead.",
                ),
            )

    def _close_setup(self) -> None:
        session, self._qr_session = self._qr_session, None
        if session is not None:
            session.ready.disconnect(self._on_qr_ready)
            session.failed.disconnect(self._on_qr_failed)
            session.close()
            session.deleteLater()
        self.publish(
            setupVisible=False,
            setupUrl="",
            accessUrl="",
            verificationCode="",
            fingerprint="",
            qrImage="",
            qrBusy=False,
            qrError="",
        )

    def _complete_setup(self) -> None:
        if self._state["setupVisible"]:
            self._settings.mark_onboarding_seen()
            self._close_setup()

    def _copy(self, value: str) -> None:
        if value.startswith("https://"):
            QGuiApplication.clipboard().setText(value)
            self.succeed(QCoreApplication.translate("SettingsWidget", "Copied"))

    def _save_certificate(self) -> None:
        setup = self._setup
        if setup is None or not self._state["setupVisible"]:
            return
        path, _ = QFileDialog.getSaveFileName(
            self._dialog_parent,
            QCoreApplication.translate("RemoteControlSetupDialog", "Save Solin certificate"),
            "solin-remote-root.cer",
            QCoreApplication.translate("RemoteControlSetupDialog", "Certificate files (*.cer)"),
        )
        if not path or self._closed or self._setup != setup or not self._state["setupVisible"]:
            return
        try:
            write_bytes_atomic(path, setup.certificate, mode=0o644)
        except OSError:
            self.fail(
                QCoreApplication.translate(
                    "RemoteControlSetupDialog", "Choose another location and try again."
                )
            )
            return
        self.succeed(QCoreApplication.translate("SettingsWidget", "Saved"))

    def cleanup(self) -> None:
        self._closed = True
        self._pending_auto_open = False
        self._setup = None
        self._close_setup()
        self.publish(password="", passwordConfirmation="", setupEnabled=False)
