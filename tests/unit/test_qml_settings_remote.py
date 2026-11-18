from __future__ import annotations

from ipaddress import IPv4Address
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from solin.core.foundation.qr_codes import generate_qr_png
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.remote_control.features import TimerRemoteFeatureFlags
from solin.core.remote_control.network import LanInterface
from solin.core.remote_control.security import (
    RemoteControlCredentialsStore,
    ScryptParameters,
    ScryptPasswordHasher,
)
from solin.core.remote_control.settings import RemoteControlSettingsStore
from solin.ui.qml.settings import remote
from solin.ui.qml.settings.domain import SettingsDomain
from solin.ui.qml.settings.navigation import SettingsNavigation
from solin.ui.qml.settings.remote import RemoteSettings


class _MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def value(self, key, default=None, value_type=None):
        value = self.values.get(key, default)
        return value_type(value) if value_type else value

    def string(self, key, default=""):
        return str(self.values.get(key, default))

    def set_value(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)


class _QrSession(QObject):
    ready = Signal(bytes)
    failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = ""
        self.close_calls = 0

    def start(self, value):
        self.started = value
        return True

    def close(self):
        self.close_calls += 1


class _QrFactory:
    def __init__(self):
        self.sessions: list[_QrSession] = []

    def create(self, *, parent=None):
        session = _QrSession(parent)
        self.sessions.append(session)
        return session


@pytest.fixture
def remote_settings(monkeypatch):
    profile = ProfileAppSettingsStore(cast(Any, _MemorySettings()))
    settings = RemoteControlSettingsStore.create(profile)
    hasher = ScryptPasswordHasher(ScryptParameters(n=1024))
    media_credentials = RemoteControlCredentialsStore.for_media(
        profile,
        password_hasher=hasher,
    )
    timer_credentials = RemoteControlCredentialsStore.for_timer(
        profile,
        password_hasher=hasher,
    )
    interface = LanInterface("wifi", "Wi-Fi", IPv4Address("192.168.1.8"))
    monkeypatch.setattr(remote, "discover_lan_interfaces", lambda: (interface,))
    factory = _QrFactory()
    domain = RemoteSettings(
        settings,
        media_credentials,
        timer_credentials,
        cast(Any, factory),
    )
    yield domain, settings, media_credentials, timer_credentials, factory, interface
    domain.cleanup()


def _save_media(domain):
    domain.setValue("mediaUsername", " Operator ")
    domain.setValue("mediaPassword", "a secure password")
    domain.setValue("mediaPasswordConfirmation", "a secure password")
    domain.invoke("saveMediaCredentials")


def _save_timer(domain):
    domain.setValue("timerUsername", " TimerOp ")
    domain.setValue("timerPassword", "another secure password")
    domain.setValue("timerPasswordConfirmation", "another secure password")
    domain.invoke("saveTimerCredentials")


def _enable_media(fixture):
    domain, _, _, _, _, interface = fixture
    domain.setValue("network", interface.selection_key)
    domain.setValue("enabled", True)
    _save_media(domain)
    domain.setValue("mediaEnabled", True)


def _running(
    domain,
    *,
    code="ABCD-EFGH",
    media_running=True,
    timer_running=False,
):
    domain.set_remote_control_runtime_status(
        running=True,
        message="Secure remote control is running.",
        media_running=media_running,
        timer_running=timer_running,
        fingerprint=":".join(f"{value:02X}" for value in range(32)),
        media_access_url="https://192.168.1.8:8765/remote/media/",
        media_setup_url="https://192.168.1.8:8765/remote/media/?setup=1",
        timer_access_url="https://192.168.1.8:8765/remote/timer/",
        timer_setup_url="https://192.168.1.8:8765/remote/timer/?setup=1",
        verification_code=code,
        certificate_der=b"certificate",
    )


def test_remote_requires_live_network_and_per_scope_credentials_before_enabling(remote_settings):
    domain, settings, _, _, _, interface = remote_settings
    changes = []
    domain.remote_control_settings_changed.connect(lambda: changes.append(True))

    domain.setValue("enabled", True)
    assert settings.enabled() is False
    assert changes == []
    assert domain.state["feedbackKind"] == "error"

    domain.setValue("mediaEnabled", True)
    assert settings.media_enabled() is False

    domain.setValue("network", interface.selection_key)
    domain.setValue("enabled", True)
    assert settings.enabled() is True

    domain.setValue("mediaEnabled", True)
    assert settings.media_enabled() is False
    domain.setValue("timerEnabled", True)
    assert settings.timer_enabled() is False

    _save_media(domain)
    domain.setValue("mediaEnabled", True)
    assert settings.media_enabled() is True

    _save_timer(domain)
    domain.setValue("timerEnabled", True)
    assert settings.timer_enabled() is True
    assert domain.state["statusKind"] == "pending"


def test_timer_feature_flag_allows_view_only_without_saved_credentials(monkeypatch):
    profile = ProfileAppSettingsStore(cast(Any, _MemorySettings()))
    settings = RemoteControlSettingsStore.create(profile)
    hasher = ScryptPasswordHasher(ScryptParameters(n=1024))
    media_credentials = RemoteControlCredentialsStore.for_media(profile, password_hasher=hasher)
    timer_credentials = RemoteControlCredentialsStore.for_timer(profile, password_hasher=hasher)
    interface = LanInterface("wifi", "Wi-Fi", IPv4Address("192.168.1.8"))
    monkeypatch.setattr(remote, "discover_lan_interfaces", lambda: (interface,))
    factory = _QrFactory()
    domain = RemoteSettings(
        settings,
        media_credentials,
        timer_credentials,
        cast(Any, factory),
        timer_feature_flags=TimerRemoteFeatureFlags(require_credentials=False),
    )
    try:
        domain.setValue("network", interface.selection_key)
        domain.setValue("enabled", True)
        domain.setValue("timerEnabled", True)
        assert settings.timer_enabled() is True
        assert domain.state["timerCredentialsSummary"] == "Optional (fullscreen view only)"
    finally:
        domain.cleanup()


@pytest.mark.parametrize(
    "username,password,confirmation",
    [
        ("ab", "secret", "secret"),
        ("operator", "short", "short"),
        ("operator", "secret", "different"),
        ("operator", "", ""),
    ],
)
def test_invalid_credentials_leave_store_unchanged(
    remote_settings, username, password, confirmation
):
    domain, _, media_credentials, _, _, _ = remote_settings
    domain.setValue("mediaUsername", username)
    domain.setValue("mediaPassword", password)
    domain.setValue("mediaPasswordConfirmation", confirmation)
    changes = []
    domain.remote_media_credentials_changed.connect(lambda: changes.append(True))
    domain.invoke("saveMediaCredentials")
    assert not media_credentials.has_credentials()
    assert changes == []
    assert domain.state["feedbackKind"] == "error"
    assert domain.state["mediaPassword"] == password


def test_credentials_commit_normalizes_username_and_clears_secrets_per_scope(remote_settings):
    domain, _, media_credentials, timer_credentials, _, _ = remote_settings
    _save_media(domain)
    _save_timer(domain)
    assert media_credentials.authenticate("operator", "a secure password")
    assert timer_credentials.authenticate("timerop", "another secure password")
    assert not media_credentials.authenticate("timerop", "another secure password")
    assert domain.state["mediaUsername"] == "operator"
    assert domain.state["timerUsername"] == "timerop"
    assert domain.state["mediaPassword"] == domain.state["mediaPasswordConfirmation"] == ""
    assert domain.state["timerPassword"] == domain.state["timerPasswordConfirmation"] == ""
    assert domain.state["mediaCredentialsSummary"] == "Configured as operator"
    assert domain.state["timerCredentialsSummary"] == "Configured as timerop"
    assert domain.state["feedbackKind"] == "success"


def test_refresh_preserves_drafts_and_cancel_discards_them(remote_settings):
    domain = remote_settings[0]
    _save_media(domain)
    domain.setValue("mediaUsername", "other")
    domain.setValue("mediaPassword", "private draft")
    domain.refresh_runtime()
    domain.refresh_language()
    assert domain.state["mediaUsername"] == "other"
    assert domain.state["mediaPassword"] == "private draft"
    domain.invoke("cancelMediaCredentials")
    assert domain.state["mediaUsername"] == "operator"
    assert domain.state["mediaPassword"] == ""


def test_credentials_groups_own_expansion_and_preserve_drafts_until_save_or_cancel(remote_settings):
    domain = remote_settings[0]
    navigation = SettingsNavigation({
        "general": SettingsDomain(),
        "integrations": SettingsDomain(),
        "remote": domain,
        "about": SettingsDomain(),
    })
    section = next(item for item in navigation.sections if item["id"] == "remote")
    media_group = next(item for item in section["groups"] if item["id"] == "remoteMedia")
    cred_group = next(item for item in media_group["children"] if item["id"] == "mediaCredentials")
    assert cred_group["collapsible"]
    assert cred_group["statusKey"] == "mediaCredentialsSummary"
    assert [row["key"] for row in cred_group["rows"]] == [
        "mediaUsername",
        "mediaPassword",
        "mediaPasswordConfirmation",
        "saveMediaCredentials",
    ]
    assert not navigation.expanded("mediaCredentials")
    domain.setValue("mediaPassword", "private draft")
    navigation.expand("mediaCredentials", True)
    navigation.expand("mediaCredentials", False)
    navigation.openSection("appearance")
    navigation.openSection("remote", "mediaCredentials", "mediaPassword")
    assert navigation.expanded("mediaCredentials")
    assert domain.state["mediaPassword"] == "private draft"
    domain.invoke("cancelMediaCredentials")
    assert domain.state["mediaPassword"] == ""
    assert navigation.expanded("mediaCredentials")
    _save_media(domain)
    assert domain.state["mediaCredentialsSummary"] == "Configured as operator"
    assert domain.state["mediaPassword"] == ""
    assert navigation.expanded("mediaCredentials")


def test_runtime_before_qml_auto_opens_setup_once_and_supports_per_scope_qr_setup(remote_settings):
    domain, _, _, _, factory, _ = remote_settings
    _enable_media(remote_settings)
    _save_timer(domain)
    domain.setValue("timerEnabled", True)
    _running(domain, media_running=True, timer_running=True)
    assert domain.state["setupVisible"]
    assert factory.sessions[0].started.endswith("/remote/timer/?setup=1")
    assert domain.state["setupTitle"] == "Set up Solin Timer"

    domain.invoke("openMediaSetup")
    assert domain.state["setupVisible"]
    assert factory.sessions[1].started.endswith("/remote/media/?setup=1")
    assert domain.state["setupTitle"] == "Set up Solin Media"

    domain.refresh_language()
    assert domain.state["statusMessage"] == "Secure remote control is running."
    assert domain.state["statusKind"] == "running"
    domain.invoke("closeSetup")
    _running(domain, media_running=True, timer_running=True)
    assert not domain.state["setupVisible"]


def test_only_done_marks_onboarding_complete_and_closes_qr_exactly_once(remote_settings):
    domain, settings, _, _, factory, _ = remote_settings
    _enable_media(remote_settings)
    _running(domain)
    domain.invoke("closeSetup")
    assert not settings.onboarding_seen()
    assert factory.sessions[0].close_calls == 1
    domain.invoke("completeSetup")
    assert not settings.onboarding_seen()
    domain.invoke("openMediaSetup")
    domain.invoke("completeSetup")
    assert settings.onboarding_seen()
    domain.cleanup()
    assert [session.close_calls for session in factory.sessions] == [1, 1]


def test_qr_validates_png_and_ignores_results_from_closed_setup(remote_settings):
    domain, _, _, _, factory, _ = remote_settings
    _enable_media(remote_settings)
    _running(domain)
    first = factory.sessions[0]
    first.ready.emit(b"broken")
    assert domain.state["qrError"]
    assert not domain.state["qrBusy"]
    first.ready.emit(generate_qr_png("https://example.test"))
    assert domain.state["qrImage"].startswith("data:image/png;base64,")
    assert not domain.state["qrError"]
    domain.invoke("closeSetup")
    domain.invoke("openMediaSetup")
    first.ready.emit(generate_qr_png("stale"))
    first.failed.emit()
    assert domain.state["qrImage"] == ""
    assert domain.state["qrBusy"]


def test_network_loss_invalidates_setup_without_erasing_persisted_selection(
    remote_settings, monkeypatch
):
    domain, settings, _, _, factory, interface = remote_settings
    _enable_media(remote_settings)
    _running(domain)
    monkeypatch.setattr(remote, "discover_lan_interfaces", lambda: ())
    domain.refresh_runtime()
    assert settings.network_selection().selection_key == interface.selection_key
    assert domain.state["network"] == ""
    assert domain.state["mediaEndpoint"] == ""
    assert domain.state["timerEndpoint"] == ""
    assert not domain.state["setupVisible"]
    assert not domain.state["mediaSetupEnabled"]
    assert factory.sessions[0].close_calls == 1


def test_disable_and_failed_runtime_close_setup_and_block_old_qr(remote_settings):
    domain, _, _, _, factory, _ = remote_settings
    _enable_media(remote_settings)
    _running(domain)
    domain.set_remote_control_runtime_status(running=False, message="Cannot bind", status="error")
    assert domain.state["statusKind"] == "error"
    assert not domain.state["setupVisible"]
    assert not domain.state["mediaSetupEnabled"]
    factory.sessions[0].failed.emit()
    assert domain.state["qrError"] == ""
    _running(domain)
    domain.invoke("openMediaSetup")
    domain.setValue("enabled", False)
    assert not domain.state["mediaSetupEnabled"]
    assert not domain.state["setupVisible"]


def test_runtime_identity_change_closes_old_setup(remote_settings):
    domain = remote_settings[0]
    _enable_media(remote_settings)
    _running(domain)
    _running(domain, code="NEW-CODE")
    assert not domain.state["setupVisible"]
    domain.invoke("openMediaSetup")
    assert domain.state["verificationCode"] == "NEW-CODE"


def test_setup_copy_and_fingerprint_preserve_canonical_values(remote_settings):
    domain = remote_settings[0]
    _enable_media(remote_settings)
    _running(domain)
    domain.invoke("copySetupAddress")
    assert QApplication.clipboard().text() == domain.state["setupUrl"]
    assert domain.state["feedbackKind"] == "success"
    fingerprint = domain.state["fingerprint"]
    assert fingerprint.replace("  ", ":") == ":".join(f"{value:02X}" for value in range(32))
    assert max(len(group) for group in fingerprint.split("  ")) == 11


def test_certificate_save_and_failure_feedback(remote_settings, monkeypatch, tmp_path):
    domain = remote_settings[0]
    _enable_media(remote_settings)
    _running(domain)
    target = tmp_path / "root.cer"
    monkeypatch.setattr(remote.QFileDialog, "getSaveFileName", lambda *_: (str(target), ""))
    domain.invoke("saveCertificate")
    assert target.read_bytes() == b"certificate"
    assert domain.state["feedbackKind"] == "success"

    def fail_write(*args, **kwargs):
        raise OSError("read-only volume")

    monkeypatch.setattr(remote, "write_bytes_atomic", fail_write)
    domain.invoke("saveCertificate")
    assert domain.state["feedbackKind"] == "error"


def test_cleanup_clears_secrets_and_ignores_late_runtime(remote_settings):
    domain = remote_settings[0]
    _enable_media(remote_settings)
    _running(domain)
    domain.setValue("mediaPassword", "draft")
    domain.setValue("timerPassword", "timer draft")
    domain.cleanup()
    _running(domain)
    domain.setValue("mediaPassword", "late draft")
    assert domain.state["mediaPassword"] == ""
    assert domain.state["mediaPasswordConfirmation"] == ""
    assert domain.state["timerPassword"] == ""
    assert domain.state["timerPasswordConfirmation"] == ""
    assert domain.state["setupUrl"] == ""
    assert not domain.state["setupVisible"]
    assert not domain.state["mediaSetupEnabled"]


def test_certificate_picker_return_after_profile_cleanup_does_not_export_stale_certificate(
    remote_settings,
    monkeypatch,
    tmp_path,
):
    domain = remote_settings[0]
    _enable_media(remote_settings)
    _running(domain)
    target = tmp_path / "stale.cer"

    def choose_after_cleanup(*args):
        domain.cleanup()
        return str(target), ""

    monkeypatch.setattr(remote.QFileDialog, "getSaveFileName", choose_after_cleanup)
    domain.invoke("saveCertificate")
    assert not target.exists()
