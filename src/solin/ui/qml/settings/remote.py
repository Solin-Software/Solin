"""Profile-scoped remote settings and device onboarding for Qt Quick."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal

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


_NoticeKind = Literal["error", "feedback"]


@dataclass(frozen=True, slots=True)
class _Notice:
    kind: _NoticeKind
    code: str


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
        self._host_notice: _Notice | None = None
        self._media_notice: _Notice | None = None
        self._media_credentials_notice: _Notice | None = None
        self._timer_notice: _Notice | None = None
        self._timer_credentials_notice: _Notice | None = None
        self._setup_notice: _Notice | None = None
        self._runtime_known = False
        self._running = False
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
            remoteFeedback="",
            remoteError="",
            mediaError="",
            mediaCredentialsFeedback="",
            mediaCredentialsError="",
            timerError="",
            timerCredentialsFeedback="",
            timerCredentialsError="",
            setupVisible=False,
            setupTitle="",
            setupEnabled=False,
            setupUrl="",
            accessUrl="",
            verificationCode="",
            fingerprint="",
            fingerprintVisible=False,
            qrImage="",
            qrBusy=False,
            qrError="",
            setupFeedback="",
            setupFeedbackKind="",
        )
        self.refresh_runtime()

    @Slot(str, "QVariant")
    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        if self._closed:
            return
        if key in {"mediaUsername", "mediaPassword", "mediaPasswordConfirmation"}:
            self._media_credentials_notice = None
            self.publish(
                **{key: str(value)},
                mediaCredentialsFeedback="",
                mediaCredentialsError="",
            )
        elif key in {"timerUsername", "timerPassword", "timerPasswordConfirmation"}:
            self._timer_credentials_notice = None
            self.publish(
                **{key: str(value)},
                timerCredentialsFeedback="",
                timerCredentialsError="",
            )
        elif key == "fingerprintVisible":
            self.publish(fingerprintVisible=bool(value))
        elif key == "network":
            interface = self._interfaces.get(str(value))
            if value and interface is None:
                self._host_notice = _Notice("error", "select_network")
                self._refresh_status()
                return
            self._host_notice = None
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
                self._host_notice = _Notice("error", "select_network_before_enable")
                self._refresh_status()
                return
            self._host_notice = None
            if not enabled:
                self._media_notice = None
                self._timer_notice = None
            else:
                if self._media_notice == _Notice("error", "enable_master_first"):
                    self._media_notice = None
                if self._timer_notice == _Notice("error", "enable_master_first"):
                    self._timer_notice = None
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
                    self._media_notice = _Notice("error", "enable_master_first")
                    self._refresh_status()
                    return
                if not self._media_credentials.has_credentials():
                    self._media_notice = _Notice("error", "save_media_credentials_first")
                    self._refresh_status()
                    return
            self._media_notice = None
            self._settings.set_media_enabled(enabled)
            if enabled and not self._settings.onboarding_seen():
                self._pending_auto_open_scope = RemoteScope.MEDIA
            self._refresh_status()
            self.remote_control_settings_changed.emit()
        elif key == "timerEnabled":
            enabled = bool(value)
            if enabled:
                if not self._settings.enabled():
                    self._timer_notice = _Notice("error", "enable_master_first")
                    self._refresh_status()
                    return
                if not self._timer_credentials_valid():
                    self._timer_notice = _Notice("error", "save_timer_credentials_first")
                    self._refresh_status()
                    return
            self._timer_notice = None
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
            "openSetup": self._open_default_setup,
            "closeSetup": self._close_setup,
            "completeSetup": self._complete_setup,
            "copySetupAddress": self._copy_setup_address,
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

    @staticmethod
    def _translate_notice(notice: _Notice | None) -> str:
        if notice is None:
            return ""
        code = notice.code
        if code == "select_network":
            return QCoreApplication.translate("SettingsWidget", "Select a private network…")
        if code == "select_network_before_enable":
            return QCoreApplication.translate(
                "SettingsWidget",
                "Select a private network before enabling remote control.",
            )
        if code == "enable_master_first":
            return QCoreApplication.translate("SettingsWidget", "Enable Remote control first.")
        if code == "save_media_credentials_first":
            return QCoreApplication.translate(
                "SettingsWidget",
                "Save Media credentials before enabling Media remote control.",
            )
        if code == "save_timer_credentials_first":
            return QCoreApplication.translate(
                "SettingsWidget",
                "Save Timer credentials before enabling Timer remote control.",
            )
        if code == "password_mismatch":
            return QCoreApplication.translate("SettingsWidget", "The passwords do not match.")
        if code == "password_empty":
            return QCoreApplication.translate("SettingsWidget", "Enter a new password to save.")
        if code == "invalid_credentials":
            return QCoreApplication.translate(
                "SettingsWidget",
                "Check the username and use a password with at least %1 characters.",
            ).replace("%1", str(PASSWORD_MIN_LENGTH))
        if code == "saved":
            return QCoreApplication.translate("SettingsWidget", "Saved")
        if code == "copied":
            return QCoreApplication.translate("SettingsWidget", "Copied")
        if code == "save_cert_failed":
            return QCoreApplication.translate(
                "RemoteControlSetupDialog",
                "Choose another location and try again.",
            )
        return ""

    @classmethod
    def _notice_pair(cls, notice: _Notice | None) -> tuple[str, str]:
        if notice is None:
            return "", ""
        text = cls._translate_notice(notice)
        if notice.kind == "error":
            return text, ""
        return "", text

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
                kind = (
                    "error"
                    if self._runtime_known and self._runtime_kind == "error"
                    else "ready"
                )
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

        if self._host_notice is not None:
            remote_error, remote_feedback = self._notice_pair(self._host_notice)
        elif enabled and network_ok and self._runtime_known and not self._running and kind == "error":
            remote_error, remote_feedback = message, ""
        elif enabled:
            remote_error, remote_feedback = "", message
        else:
            remote_error, remote_feedback = "", ""

        media_cred_error, media_cred_feedback = self._notice_pair(self._media_credentials_notice)
        timer_cred_error, timer_cred_feedback = self._notice_pair(self._timer_credentials_notice)
        setup_feedback = self._translate_notice(self._setup_notice)
        setup_feedback_kind = (
            ""
            if self._setup_notice is None
            else ("error" if self._setup_notice.kind == "error" else "success")
        )

        origin = f"https://{interface.ipv4_address}:{REMOTE_CONTROL_PORT}" if interface else ""
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
            passwordHint=QCoreApplication.translate(
                "SettingsWidget",
                "Use at least %1 characters. Credentials belong only to this profile.",
            ).replace("%1", str(PASSWORD_MIN_LENGTH)),
            mediaEndpoint=f"{origin}{REMOTE_MEDIA_PREFIX}/" if origin else "",
            timerEndpoint=f"{origin}{REMOTE_TIMER_PREFIX}/" if origin else "",
            statusMessage=message,
            statusKind=kind,
            remoteError=remote_error,
            remoteFeedback=remote_feedback,
            mediaError=self._translate_notice(self._media_notice),
            mediaCredentialsError=media_cred_error,
            mediaCredentialsFeedback=media_cred_feedback,
            timerError=self._translate_notice(self._timer_notice),
            timerCredentialsError=timer_cred_error,
            timerCredentialsFeedback=timer_cred_feedback,
            setupFeedback=setup_feedback,
            setupFeedbackKind=setup_feedback_kind,
            setupEnabled=self._default_setup_target() is not None,
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

    def _invalidate_runtime(self) -> None:
        self._runtime_known = False
        self._running = False
        self._runtime_message = ""
        self._runtime_kind = "pending"
        self._media_setup = None
        self._timer_setup = None
        self._close_setup()
        self.publish(setupEnabled=False)

    def _cancel_media_credentials(self) -> None:
        self._media_credentials_notice = None
        self.publish(
            mediaUsername=self._media_credentials.configured_username(),
            mediaPassword="",
            mediaPasswordConfirmation="",
            mediaCredentialsError="",
            mediaCredentialsFeedback="",
        )

    def _cancel_timer_credentials(self) -> None:
        self._timer_credentials_notice = None
        self.publish(
            timerUsername=self._timer_credentials.configured_username(),
            timerPassword="",
            timerPasswordConfirmation="",
            timerCredentialsError="",
            timerCredentialsFeedback="",
        )

    def _save_media_credentials(self) -> None:
        error_code = self._persist_credentials(
            store=self._media_credentials,
            username_key="mediaUsername",
            password_key="mediaPassword",
            confirm_key="mediaPasswordConfirmation",
        )
        if error_code is not None:
            self._media_credentials_notice = _Notice("error", error_code)
            self._refresh_status()
            return
        self._cancel_media_credentials()
        self._media_credentials_notice = _Notice("feedback", "saved")
        if self._media_notice == _Notice("error", "save_media_credentials_first"):
            self._media_notice = None
        self._refresh_status()
        self.remote_media_credentials_changed.emit()

    def _save_timer_credentials(self) -> None:
        error_code = self._persist_credentials(
            store=self._timer_credentials,
            username_key="timerUsername",
            password_key="timerPassword",
            confirm_key="timerPasswordConfirmation",
        )
        if error_code is not None:
            self._timer_credentials_notice = _Notice("error", error_code)
            self._refresh_status()
            return
        self._cancel_timer_credentials()
        self._timer_credentials_notice = _Notice("feedback", "saved")
        if self._timer_notice == _Notice("error", "save_timer_credentials_first"):
            self._timer_notice = None
        self._refresh_status()
        self.remote_timer_credentials_changed.emit()

    def _persist_credentials(
        self,
        *,
        store: RemoteControlCredentialsStore,
        username_key: str,
        password_key: str,
        confirm_key: str,
    ) -> str | None:
        password = str(self._state.get(password_key, ""))
        if password != str(self._state.get(confirm_key, "")):
            return "password_mismatch"
        if not password:
            return "password_empty"
        try:
            store.set_credentials(str(self._state.get(username_key, "")), password)
        except (UsernameValidationError, PasswordValidationError):
            return "invalid_credentials"
        return None

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
        self._runtime_message = message
        self._runtime_kind = "running" if running else status or "error"
        self._refresh_status()

        pending = self._pending_auto_open_scope
        if pending is not None:
            target = next_media if pending is RemoteScope.MEDIA else next_timer
            if target is not None:
                self._pending_auto_open_scope = None
                self._open_setup(self._setup_target(pending))

    def _setup_target(self, scope: RemoteScope) -> _SetupTarget | None:
        if not self._settings.enabled():
            return None
        if scope is RemoteScope.MEDIA:
            return self._media_setup if self._settings.media_enabled() else None
        return self._timer_setup if self._settings.timer_enabled() else None

    def _default_setup_target(self) -> _SetupTarget | None:
        return self._setup_target(RemoteScope.MEDIA) or self._setup_target(RemoteScope.TIMER)

    def _open_default_setup(self) -> None:
        self._open_setup(self._default_setup_target())

    def _open_setup(self, setup: _SetupTarget | None) -> None:
        if setup is None:
            return
        if self._state["setupVisible"]:
            self._close_setup()
        self._active_setup = setup
        self._setup_notice = None
        octets = setup.fingerprint.split(":")
        fingerprint = (
            "  ".join(":".join(octets[index : index + 4]) for index in range(0, len(octets), 4))
            if len(octets) > 1 and all(len(octet) == 2 for octet in octets)
            else setup.fingerprint
        )
        self.publish(
            setupVisible=True,
            setupTitle=self._setup_dialog_title(setup.scope),
            setupUrl=setup.setup_url,
            accessUrl=setup.access_url,
            verificationCode=setup.verification_code,
            fingerprint=fingerprint,
            fingerprintVisible=False,
            qrImage="",
            qrBusy=True,
            qrError="",
            setupFeedback="",
            setupFeedbackKind="",
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
        self._setup_notice = None
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
            setupFeedback="",
            setupFeedbackKind="",
        )

    def _complete_setup(self) -> None:
        if self._state["setupVisible"]:
            self._settings.mark_onboarding_seen()
            self._close_setup()

    @staticmethod
    def _copy_https_address(value: str) -> bool:
        if not value.startswith("https://"):
            return False
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(value)
        return True

    @Slot(str, result=bool)
    def copyAddress(self, key: str) -> bool:  # noqa: N802 - QML API
        if self._closed or key not in {"mediaEndpoint", "timerEndpoint"}:
            return False
        return self._copy_https_address(str(self._state.get(key, "")))

    def _copy_setup_address(self) -> None:
        if not self._copy_https_address(str(self._state.get("setupUrl", ""))):
            return
        self._setup_notice = _Notice("feedback", "copied")
        self._refresh_status()

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
            self._setup_notice = _Notice("error", "save_cert_failed")
            self._refresh_status()
            return
        self._setup_notice = _Notice("feedback", "saved")
        self._refresh_status()

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
        )
