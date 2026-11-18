"""Profile-scoped remote settings and device onboarding for Qt Quick."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QFileDialog, QWidget

from solin.core.remote_control.features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
)
from solin.core.remote_control.network import LanInterface, discover_lan_interfaces
from solin.core.remote_control.security import (
    PASSWORD_MIN_LENGTH,
    PasswordValidationError,
    RemoteControlCredentialsStore,
    RemoteScope,
    UsernameValidationError,
)
from solin.core.remote_control.server import REMOTE_MEDIA_PREFIX, REMOTE_TIMER_PREFIX
from solin.core.remote_control.settings import REMOTE_CONTROL_PORT, RemoteControlSettingsStore
from solin.core.storage.binary_files import write_bytes_atomic
from solin.ui.qr_generation import QrGenerationSession, QrGenerationSessionFactory

from .domain import SettingsDomain


@dataclass(frozen=True, slots=True)
class _SetupTarget:
    scope: RemoteScope
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
    remote_media_credentials_changed = Signal()
    remote_timer_credentials_changed = Signal()

    def __init__(
        self,
        remote_control_settings: RemoteControlSettingsStore,
        remote_media_credentials: RemoteControlCredentialsStore,
        remote_timer_credentials: RemoteControlCredentialsStore,
        qr_generation_session_factory: QrGenerationSessionFactory,
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
        *,
        timer_feature_flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    ) -> None:
        super().__init__(parent)
        self._settings = remote_control_settings
        self._media_credentials = remote_media_credentials
        self._timer_credentials = remote_timer_credentials
        self._timer_feature_flags = timer_feature_flags
        self._qr_factory = qr_generation_session_factory
        self._dialog_parent = dialog_parent
        self._interfaces: dict[str, LanInterface] = {}
        self._media_setup: _SetupTarget | None = None
        self._timer_setup: _SetupTarget | None = None
        self._active_setup: _SetupTarget | None = None
        self._qr_session: QrGenerationSession | None = None
        self._pending_auto_open_scope: RemoteScope | None = None
        self._runtime_known = False
        self._running = False
        self._media_running = False
        self._timer_running = False
        self._runtime_message = ""
        self._runtime_kind = "pending"
        self._closed = False
        self.publish(
            mediaUsername=self._media_credentials.configured_username(),
            mediaPassword="",
            mediaPasswordConfirmation="",
            timerUsername=self._timer_credentials.configured_username(),
            timerPassword="",
            timerPasswordConfirmation="",
            setupVisible=False,
            setupTitle="",
            setupEnabled=False,
            mediaSetupEnabled=False,
            timerSetupEnabled=False,
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
        if key in {
            "mediaUsername",
            "mediaPassword",
            "mediaPasswordConfirmation",
            "timerUsername",
            "timerPassword",
            "timerPasswordConfirmation",
        }:
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
            if enabled and not self._network_valid():
                self.fail(
                    QCoreApplication.translate(
                        "SettingsWidget",
                        "Select a private network before enabling remote control.",
                    )
                )
                return
            self._settings.set_enabled(enabled)
            if enabled and not self._settings.onboarding_seen():
                if self._settings.media_enabled():
                    self._pending_auto_open_scope = RemoteScope.MEDIA
                elif self._settings.timer_enabled():
                    self._pending_auto_open_scope = RemoteScope.TIMER
            self._invalidate_runtime()
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        elif key == "mediaEnabled":
            enabled = bool(value)
            if enabled:
                if not self._settings.enabled():
                    self.fail(
                        QCoreApplication.translate(
                            "SettingsWidget",
                            "Enable Remote control first.",
                        )
                    )
                    return
                if not self._media_credentials.has_credentials():
                    self.fail(
                        QCoreApplication.translate(
                            "SettingsWidget",
                            "Save Media credentials before enabling Media remote control.",
                        )
                    )
                    return
            self._settings.set_media_enabled(enabled)
            if enabled and not self._settings.onboarding_seen():
                self._pending_auto_open_scope = RemoteScope.MEDIA
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        elif key == "timerEnabled":
            enabled = bool(value)
            if enabled:
                if not self._settings.enabled():
                    self.fail(
                        QCoreApplication.translate(
                            "SettingsWidget",
                            "Enable Remote control first.",
                        )
                    )
                    return
                if not self._timer_credentials_valid():
                    self.fail(
                        QCoreApplication.translate(
                            "SettingsWidget",
                            "Save Timer credentials before enabling Timer remote control.",
                        )
                    )
                    return
            self._settings.set_timer_enabled(enabled)
            if enabled and not self._settings.onboarding_seen():
                self._pending_auto_open_scope = RemoteScope.TIMER
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        else:
            super().setValue(key, value)

    @Slot(str)
    def invoke(self, action: str) -> None:
        if self._closed:
            return
        actions = {
            "cancelMediaCredentials": self._cancel_media_credentials,
            "saveMediaCredentials": self._save_media_credentials,
            "cancelTimerCredentials": self._cancel_timer_credentials,
            "saveTimerCredentials": self._save_timer_credentials,
            "copyMediaAddress": lambda: self._copy(self._state.get("mediaEndpoint", "")),
            "copyTimerAddress": lambda: self._copy(self._state.get("timerEndpoint", "")),
            "openSetup": self._open_default_setup,
            "openMediaSetup": lambda: self._open_scope_setup(RemoteScope.MEDIA),
            "openTimerSetup": lambda: self._open_scope_setup(RemoteScope.TIMER),
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

    def _network_valid(self) -> bool:
        return self._selection_key() in self._interfaces

    def _timer_credentials_valid(self) -> bool:
        return (
            self._timer_credentials.has_credentials()
            or not self._timer_feature_flags.require_credentials
        )

    def refresh_runtime(self) -> None:
        if self._closed:
            return
        self._interfaces = {item.selection_key: item for item in discover_lan_interfaces()}
        if not self._network_valid():
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
        if self._active_setup is not None and self._state["setupVisible"]:
            self.publish(setupTitle=self._setup_dialog_title(self._active_setup.scope))
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
        network_ok = interface is not None
        enabled = self._settings.enabled()
        media_enabled = self._settings.media_enabled()
        timer_enabled = self._settings.timer_enabled()
        media_configured = self._media_credentials.has_credentials()
        timer_configured = self._timer_credentials.has_credentials()
        timer_valid = self._timer_credentials_valid()

        any_service_eligible = (media_enabled and media_configured) or (
            timer_enabled and timer_valid
        )
        if enabled and network_ok:
            if any_service_eligible:
                message = (
                    self._runtime_message
                    if self._runtime_known
                    else QCoreApplication.translate(
                        "SettingsWidget", "Starting secure remote control…"
                    )
                )
                kind = self._runtime_kind if self._runtime_known else "pending"
            else:
                message = (
                    self._runtime_message
                    if self._runtime_known and self._runtime_message
                    else QCoreApplication.translate(
                        "SettingsWidget",
                        "Enable Media or Timer remote control to start accepting connections.",
                    )
                )
                kind = "ready"
        elif network_ok:
            message, kind = (
                QCoreApplication.translate("SettingsWidget", "Ready to enable."),
                "ready",
            )
        else:
            message, kind = (
                QCoreApplication.translate("SettingsWidget", "Configuration required."),
                "pending",
            )

        origin = f"https://{interface.ipv4_address}:{REMOTE_CONTROL_PORT}" if interface else ""
        media_setup_ready = bool(
            enabled
            and media_enabled
            and self._running
            and self._media_running
            and self._media_setup is not None
            and self._media_setup.ready
        )
        timer_setup_ready = bool(
            enabled
            and timer_enabled
            and self._running
            and self._timer_running
            and self._timer_setup is not None
            and self._timer_setup.ready
        )

        self.publish(
            enabled=enabled,
            mediaEnabled=media_enabled,
            timerEnabled=timer_enabled,
            network=selected if interface else "",
            mediaCredentialsConfigured=media_configured,
            timerCredentialsConfigured=timer_configured,
            mediaCredentialsSummary=self._credentials_summary(self._media_credentials),
            timerCredentialsSummary=self._credentials_summary(
                self._timer_credentials,
                allow_view_only=not self._timer_feature_flags.require_credentials,
            ),
            mediaStatusSummary=self._service_status_summary(
                master_enabled=enabled,
                service_enabled=media_enabled,
                service_running=self._media_running,
                credentials_valid=media_configured,
            ),
            timerStatusSummary=self._service_status_summary(
                master_enabled=enabled,
                service_enabled=timer_enabled,
                service_running=self._timer_running,
                credentials_valid=timer_valid,
            ),
            passwordHint=QCoreApplication.translate(
                "SettingsWidget",
                "Use at least %1 characters. Credentials belong only to this profile.",
            ).replace("%1", str(PASSWORD_MIN_LENGTH)),
            mediaEndpoint=f"{origin}{REMOTE_MEDIA_PREFIX}/" if origin else "",
            timerEndpoint=f"{origin}{REMOTE_TIMER_PREFIX}/" if origin else "",
            statusMessage=message,
            statusKind=kind,
            mediaSetupEnabled=media_setup_ready,
            timerSetupEnabled=timer_setup_ready,
            setupEnabled=bool(media_setup_ready or timer_setup_ready),
        )

    @staticmethod
    def _credentials_summary(
        store: RemoteControlCredentialsStore,
        *,
        allow_view_only: bool = False,
    ) -> str:
        if store.has_credentials():
            return QCoreApplication.translate("SettingsWidget", "Configured as %1").replace(
                "%1", store.configured_username()
            )
        if allow_view_only:
            return QCoreApplication.translate("SettingsWidget", "Optional (fullscreen view only)")
        return QCoreApplication.translate("SettingsWidget", "Not configured")

    def _service_status_summary(
        self,
        *,
        master_enabled: bool,
        service_enabled: bool,
        service_running: bool,
        credentials_valid: bool,
    ) -> str:
        if not master_enabled or not service_enabled:
            return QCoreApplication.translate("SettingsWidget", "Disabled")
        if not credentials_valid:
            return QCoreApplication.translate("SettingsWidget", "Credentials required")
        if self._running and service_running:
            return QCoreApplication.translate("SettingsWidget", "Active")
        return QCoreApplication.translate("SettingsWidget", "Starting…")

    def _invalidate_runtime(self) -> None:
        self._runtime_known = False
        self._running = False
        self._media_running = False
        self._timer_running = False
        self._runtime_message = ""
        self._runtime_kind = "pending"
        self._media_setup = None
        self._timer_setup = None
        self._close_setup()
        self.publish(
            setupEnabled=False,
            mediaSetupEnabled=False,
            timerSetupEnabled=False,
        )

    def _cancel_media_credentials(self) -> None:
        self.publish(
            mediaUsername=self._media_credentials.configured_username(),
            mediaPassword="",
            mediaPasswordConfirmation="",
            feedback="",
            feedbackKind="",
        )

    def _cancel_timer_credentials(self) -> None:
        self.publish(
            timerUsername=self._timer_credentials.configured_username(),
            timerPassword="",
            timerPasswordConfirmation="",
            feedback="",
            feedbackKind="",
        )

    def _save_media_credentials(self) -> None:
        if not self._persist_credentials(
            store=self._media_credentials,
            username_key="mediaUsername",
            password_key="mediaPassword",
            confirm_key="mediaPasswordConfirmation",
        ):
            return
        self._cancel_media_credentials()
        self._refresh_status()
        self.remote_media_credentials_changed.emit()
        self.succeed(QCoreApplication.translate("SettingsWidget", "Saved"))

    def _save_timer_credentials(self) -> None:
        if not self._persist_credentials(
            store=self._timer_credentials,
            username_key="timerUsername",
            password_key="timerPassword",
            confirm_key="timerPasswordConfirmation",
        ):
            return
        self._cancel_timer_credentials()
        self._refresh_status()
        self.remote_timer_credentials_changed.emit()
        self.succeed(QCoreApplication.translate("SettingsWidget", "Saved"))

    def _persist_credentials(
        self,
        *,
        store: RemoteControlCredentialsStore,
        username_key: str,
        password_key: str,
        confirm_key: str,
    ) -> bool:
        password = str(self._state.get(password_key, ""))
        if password != str(self._state.get(confirm_key, "")):
            self.fail(QCoreApplication.translate("SettingsWidget", "The passwords do not match."))
            return False
        if not password:
            self.fail(QCoreApplication.translate("SettingsWidget", "Enter a new password to save."))
            return False
        try:
            store.set_credentials(str(self._state.get(username_key, "")), password)
        except (UsernameValidationError, PasswordValidationError):
            self.fail(
                QCoreApplication.translate(
                    "SettingsWidget",
                    "Check the username and use a password with at least %1 characters.",
                ).replace("%1", str(PASSWORD_MIN_LENGTH))
            )
            return False
        return True

    def set_remote_control_runtime_status(
        self,
        *,
        running: bool,
        message: str,
        media_running: bool = False,
        timer_running: bool = False,
        fingerprint: str = "",
        media_access_url: str = "",
        media_setup_url: str = "",
        timer_access_url: str = "",
        timer_setup_url: str = "",
        verification_code: str = "",
        certificate_der: bytes = b"",
        status: str | None = None,
    ) -> None:
        if self._closed:
            return
        media_candidate = _SetupTarget(
            RemoteScope.MEDIA,
            media_access_url,
            media_setup_url,
            verification_code,
            fingerprint,
            certificate_der,
        )
        timer_candidate = _SetupTarget(
            RemoteScope.TIMER,
            timer_access_url,
            timer_setup_url,
            verification_code,
            fingerprint,
            certificate_der,
        )
        next_media = media_candidate if running and media_running and media_candidate.ready else None
        next_timer = timer_candidate if running and timer_running and timer_candidate.ready else None
        if self._active_setup is not None:
            current_for_scope = (
                next_media if self._active_setup.scope is RemoteScope.MEDIA else next_timer
            )
            if current_for_scope != self._active_setup:
                self._close_setup()
        self._media_setup = next_media
        self._timer_setup = next_timer
        self._runtime_known = True
        self._running = running
        self._media_running = bool(running and media_running)
        self._timer_running = bool(running and timer_running)
        self._runtime_message = message
        self._runtime_kind = "running" if running else status or "error"
        self._refresh_status()

        pending = self._pending_auto_open_scope
        if pending is not None:
            target = next_media if pending is RemoteScope.MEDIA else next_timer
            if target is not None and target.ready:
                self._pending_auto_open_scope = None
                self._open_scope_setup(pending)

    def _open_default_setup(self) -> None:
        if self._state.get("mediaSetupEnabled"):
            self._open_scope_setup(RemoteScope.MEDIA)
        elif self._state.get("timerSetupEnabled"):
            self._open_scope_setup(RemoteScope.TIMER)

    def _open_scope_setup(self, scope: RemoteScope) -> None:
        setup = self._media_setup if scope is RemoteScope.MEDIA else self._timer_setup
        enabled_flag = (
            "mediaSetupEnabled" if scope is RemoteScope.MEDIA else "timerSetupEnabled"
        )
        if not self._state.get(enabled_flag) or setup is None:
            return
        if self._state["setupVisible"]:
            self._close_setup()
        self._active_setup = setup
        octets = setup.fingerprint.split(":")
        fingerprint = (
            "  ".join(":".join(octets[index : index + 4]) for index in range(0, len(octets), 4))
            if len(octets) > 1 and all(len(octet) == 2 for octet in octets)
            else setup.fingerprint
        )
        self.publish(
            setupVisible=True,
            setupTitle=self._setup_dialog_title(scope),
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

    @staticmethod
    def _setup_dialog_title(scope: RemoteScope) -> str:
        if scope is RemoteScope.TIMER:
            return QCoreApplication.translate(
                "RemoteControlSetupDialog", "Set up Solin Timer"
            )
        return QCoreApplication.translate(
            "RemoteControlSetupDialog", "Set up Solin Media"
        )

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
        if QImage.fromData(data, "PNG").isNull():  # type: ignore[arg-type]
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
        self._active_setup = None
        session, self._qr_session = self._qr_session, None
        if session is not None:
            session.ready.disconnect(self._on_qr_ready)
            session.failed.disconnect(self._on_qr_failed)
            session.close()
            session.deleteLater()
        self.publish(
            setupVisible=False,
            setupTitle="",
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
        setup = self._active_setup
        if setup is None or not self._state["setupVisible"]:
            return
        path, _ = QFileDialog.getSaveFileName(
            self._dialog_parent,
            QCoreApplication.translate("RemoteControlSetupDialog", "Save Solin certificate"),
            "solin-remote-root.cer",
            QCoreApplication.translate("RemoteControlSetupDialog", "Certificate files (*.cer)"),
        )
        if (
            not path
            or self._closed
            or self._active_setup != setup
            or not self._state["setupVisible"]
        ):
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
        self._pending_auto_open_scope = None
        self._media_setup = None
        self._timer_setup = None
        self._close_setup()
        self.publish(
            mediaPassword="",
            mediaPasswordConfirmation="",
            timerPassword="",
            timerPasswordConfirmation="",
            setupEnabled=False,
            mediaSetupEnabled=False,
            timerSetupEnabled=False,
        )
