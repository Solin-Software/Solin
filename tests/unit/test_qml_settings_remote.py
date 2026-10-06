from __future__ import annotations

from ipaddress import IPv4Address
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from solin.core.foundation.qr_codes import generate_qr_png
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.remote_control.network import LanInterface
from solin.core.remote_control.security import (
    RemoteControlCredentialsStore,
    ScryptParameters,
    ScryptPasswordHasher,
)
from solin.core.remote_control.settings import RemoteControlSettingsStore
from solin.ui.qml.settings import remote
from solin.ui.qml.settings.remote import RemoteSettings
from solin.ui.qml.settings.domain import SettingsDomain
from solin.ui.qml.settings.navigation import SettingsNavigation


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
    credentials = RemoteControlCredentialsStore.create(
        profile,
        password_hasher=ScryptPasswordHasher(ScryptParameters(n=1024)),
    )
    interface = LanInterface("wifi", "Wi-Fi", IPv4Address("192.168.1.8"))
    monkeypatch.setattr(remote, "discover_lan_interfaces", lambda: (interface,))
    factory = _QrFactory()
    domain = RemoteSettings(settings, credentials, cast(Any, factory))
    yield domain, settings, credentials, factory, interface
    domain.cleanup()


def _save(domain):
    domain.setValue("username", " Operator ")
    domain.setValue("password", "a secure password")
    domain.setValue("passwordConfirmation", "a secure password")
    domain.invoke("saveCredentials")


def _enable(fixture):
    domain, _, _, _, interface = fixture
    domain.setValue("network", interface.selection_key)
    _save(domain)
    domain.setValue("enabled", True)


def _running(domain, *, code="ABCD-EFGH"):
    domain.set_remote_control_runtime_status(
        running=True,
        message="Secure remote control is running.",
        fingerprint=":".join(f"{value:02X}" for value in range(32)),
        access_url="https://192.168.1.8:8765/remote/",
        setup_url="https://192.168.1.8:8765/remote/?setup=1",
        verification_code=code,
        certificate_der=b"certificate",
    )


def test_remote_requires_live_network_and_saved_credentials_before_enabling(remote_settings):
    domain, settings, _, _, interface = remote_settings
    changes = []
    domain.remote_control_settings_changed.connect(lambda: changes.append(True))
    domain.setValue("enabled", True)
    assert settings.enabled() is False
    assert changes == []
    assert domain.state["feedbackKind"] == "error"
    domain.setValue("network", interface.selection_key)
    domain.setValue("enabled", True)
    assert settings.enabled() is False
    _save(domain)
    domain.setValue("enabled", True)
    assert settings.enabled() is True
    assert changes == [True, True]
    assert domain.state["statusKind"] == "pending"


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
    domain, _, credentials, _, _ = remote_settings
    domain.setValue("username", username)
    domain.setValue("password", password)
    domain.setValue("passwordConfirmation", confirmation)
    changes = []
    domain.remote_control_credentials_changed.connect(lambda: changes.append(True))
    domain.invoke("saveCredentials")
    assert not credentials.has_credentials()
    assert changes == []
    assert domain.state["feedbackKind"] == "error"
    assert domain.state["password"] == password


def test_credentials_commit_normalizes_username_and_clears_secrets(remote_settings):
    domain, _, credentials, _, _ = remote_settings
    _save(domain)
    assert credentials.authenticate("operator", "a secure password")
    assert domain.state["username"] == "operator"
    assert domain.state["password"] == domain.state["passwordConfirmation"] == ""
    assert domain.state["credentialsSummary"] == "Configured as operator"
    assert domain.state["feedbackKind"] == "success"


def test_refresh_preserves_drafts_and_cancel_discards_them(remote_settings):
    domain = remote_settings[0]
    _save(domain)
    domain.setValue("username", "other")
    domain.setValue("password", "private draft")
    domain.refresh_runtime()
    domain.refresh_language()
    assert domain.state["username"] == "other"
    assert domain.state["password"] == "private draft"
    domain.invoke("cancelCredentials")
    assert domain.state["username"] == "operator"
    assert domain.state["password"] == ""


def test_credentials_group_owns_expansion_and_preserves_drafts_until_save_or_cancel(remote_settings):
    domain = remote_settings[0]
    navigation = SettingsNavigation({
        "general": SettingsDomain(), "integrations": SettingsDomain(), "remote": domain,
    })
    section = next(item for item in navigation.sections if item["id"] == "remote")
    group = next(item for item in section["groups"] if item["id"] == "credentials")
    assert group["collapsible"]
    assert group["statusKey"] == "credentialsSummary"
    assert [row["key"] for row in group["rows"]] == [
        "username", "password", "passwordConfirmation", "saveCredentials",
    ]
    assert not any(row["visibleWhen"] for row in group["rows"])
    assert not navigation.expanded("credentials")
    domain.setValue("password", "private draft")
    navigation.expand("credentials", True)
    navigation.expand("credentials", False)
    navigation.openSection("appearance")
    navigation.openSection("remote", "credentials", "password")
    assert navigation.expanded("credentials")
    assert domain.state["password"] == "private draft"
    domain.invoke("cancelCredentials")
    assert domain.state["password"] == ""
    assert navigation.expanded("credentials")
    _save(domain)
    assert domain.state["credentialsSummary"] == "Configured as operator"
    assert domain.state["password"] == ""
    assert navigation.expanded("credentials")


def test_runtime_before_qml_auto_opens_setup_once_and_refresh_preserves_status(remote_settings):
    domain, _, _, factory, _ = remote_settings
    _enable(remote_settings)
    _running(domain)
    assert domain.state["setupVisible"]
    assert factory.sessions[0].started.endswith("/remote/?setup=1")
    domain.refresh_language()
    assert domain.state["statusMessage"] == "Secure remote control is running."
    assert domain.state["statusKind"] == "running"
    domain.invoke("closeSetup")
    _running(domain)
    assert not domain.state["setupVisible"]
    assert len(factory.sessions) == 1


def test_only_done_marks_onboarding_complete_and_closes_qr_exactly_once(remote_settings):
    domain, settings, _, factory, _ = remote_settings
    _enable(remote_settings)
    _running(domain)
    domain.invoke("closeSetup")
    assert not settings.onboarding_seen()
    assert factory.sessions[0].close_calls == 1
    domain.invoke("completeSetup")
    assert not settings.onboarding_seen()
    domain.invoke("openSetup")
    domain.invoke("completeSetup")
    assert settings.onboarding_seen()
    domain.cleanup()
    assert [session.close_calls for session in factory.sessions] == [1, 1]


def test_qr_validates_png_and_ignores_results_from_closed_setup(remote_settings):
    domain, _, _, factory, _ = remote_settings
    _enable(remote_settings)
    _running(domain)
    first = factory.sessions[0]
    first.ready.emit(b"broken")
    assert domain.state["qrError"]
    assert not domain.state["qrBusy"]
    first.ready.emit(generate_qr_png("https://example.test"))
    assert domain.state["qrImage"].startswith("data:image/png;base64,")
    assert not domain.state["qrError"]
    domain.invoke("closeSetup")
    domain.invoke("openSetup")
    first.ready.emit(generate_qr_png("stale"))
    first.failed.emit()
    assert domain.state["qrImage"] == ""
    assert domain.state["qrBusy"]


def test_network_loss_invalidates_setup_without_erasing_persisted_selection(
    remote_settings, monkeypatch
):
    domain, settings, _, factory, interface = remote_settings
    _enable(remote_settings)
    _running(domain)
    monkeypatch.setattr(remote, "discover_lan_interfaces", lambda: ())
    domain.refresh_runtime()
    assert settings.network_selection().selection_key == interface.selection_key
    assert domain.state["network"] == ""
    assert domain.state["endpoint"] == ""
    assert not domain.state["setupVisible"]
    assert not domain.state["setupEnabled"]
    assert factory.sessions[0].close_calls == 1


def test_disable_and_failed_runtime_close_setup_and_block_old_qr(remote_settings):
    domain, _, _, factory, _ = remote_settings
    _enable(remote_settings)
    _running(domain)
    domain.set_remote_control_runtime_status(running=False, message="Cannot bind", status="error")
    assert domain.state["statusKind"] == "error"
    assert not domain.state["setupVisible"]
    assert not domain.state["setupEnabled"]
    factory.sessions[0].failed.emit()
    assert domain.state["qrError"] == ""
    _running(domain)
    domain.invoke("openSetup")
    domain.setValue("enabled", False)
    assert not domain.state["setupEnabled"]
    assert not domain.state["setupVisible"]


def test_runtime_identity_change_closes_old_setup(remote_settings):
    domain = remote_settings[0]
    _enable(remote_settings)
    _running(domain)
    _running(domain, code="NEW-CODE")
    assert not domain.state["setupVisible"]
    domain.invoke("openSetup")
    assert domain.state["verificationCode"] == "NEW-CODE"


def test_setup_copy_and_fingerprint_preserve_canonical_values(remote_settings):
    domain = remote_settings[0]
    _enable(remote_settings)
    _running(domain)
    domain.invoke("copySetupAddress")
    assert QApplication.clipboard().text() == domain.state["setupUrl"]
    assert domain.state["feedbackKind"] == "success"
    fingerprint = domain.state["fingerprint"]
    assert fingerprint.replace("  ", ":") == ":".join(f"{value:02X}" for value in range(32))
    assert max(len(group) for group in fingerprint.split("  ")) == 11


def test_certificate_save_and_failure_feedback(remote_settings, monkeypatch, tmp_path):
    domain = remote_settings[0]
    _enable(remote_settings)
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
    _enable(remote_settings)
    _running(domain)
    domain.setValue("password", "draft")
    domain.cleanup()
    _running(domain)
    domain.setValue("password", "late draft")
    assert domain.state["password"] == ""
    assert domain.state["passwordConfirmation"] == ""
    assert domain.state["setupUrl"] == ""
    assert not domain.state["setupVisible"]
    assert not domain.state["setupEnabled"]


def test_certificate_picker_return_after_profile_cleanup_does_not_export_stale_certificate(
    remote_settings,
    monkeypatch,
    tmp_path,
):
    domain = remote_settings[0]
    _enable(remote_settings)
    _running(domain)
    target = tmp_path / "stale.cer"

    def choose_after_cleanup(*args):
        domain.cleanup()
        return str(target), ""

    monkeypatch.setattr(remote.QFileDialog, "getSaveFileName", choose_after_cleanup)
    domain.invoke("saveCertificate")
    assert not target.exists()
