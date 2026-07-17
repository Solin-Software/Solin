from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.remote_control.security import (
    DEFAULT_SCRYPT_PARAMETERS,
    PASSWORD_MAX_LENGTH,
    InMemorySessionStore,
    LoginRateLimiter,
    LoginRateLimitPolicy,
    PasswordValidationError,
    RemoteControlCredentialsStore,
    ScryptParameters,
    ScryptPasswordHasher,
    SessionGenerationChangedError,
    SessionLimitReachedError,
    UsernameValidationError,
    normalize_username,
    validate_password,
)
from solin.core.remote_control.settings import (
    REMOTE_CONTROL_PORT,
    RemoteControlSettingsStore,
)


class _MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def string(self, key: str, default: str = "") -> str:
        return str(self.values.get(key, default) or "")

    def value(
        self,
        key: str,
        default: object = None,
        value_type: type | None = None,
    ) -> object:
        value = self.values.get(key, default)
        return value_type(value) if value_type is not None else value

    def set_value(self, key: str, value: object, *, sync: bool = True) -> None:
        del sync
        self.values[key] = value

    def remove(self, key: str, *, sync: bool = True) -> None:
        del sync
        self.values.pop(key, None)


@dataclass(slots=True)
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


def _credentials_store() -> tuple[RemoteControlCredentialsStore, _MemorySettings]:
    settings = _MemorySettings()
    profile_settings = ProfileAppSettingsStore(cast(Any, settings))
    hasher = ScryptPasswordHasher(
        ScryptParameters(n=1_024, r=8, p=1, max_memory_bytes=8 * 1024 * 1024),
        salt_factory=lambda length: b"s" * length,
    )
    return RemoteControlCredentialsStore.create(
        profile_settings,
        password_hasher=hasher,
    ), settings


def test_username_and_password_validation_preserve_clear_contracts() -> None:
    assert normalize_username("  Control.Room_1 ") == "control.room_1"
    assert validate_password(" correct horse battery staple ") == (" correct horse battery staple ")

    for invalid in ("ab", "has spaces", "álvaro", "-operator", "operator!"):
        with pytest.raises(UsernameValidationError):
            normalize_username(invalid)

    for invalid_password in ("too-short", "valid-length\n", "x" * (PASSWORD_MAX_LENGTH + 1)):
        with pytest.raises(PasswordValidationError):
            validate_password(invalid_password)


def test_default_password_hash_uses_required_scrypt_cost() -> None:
    assert DEFAULT_SCRYPT_PARAMETERS == ScryptParameters(
        n=32_768,
        r=8,
        p=1,
        length=32,
        salt_length=16,
        max_memory_bytes=64 * 1024 * 1024,
    )


def test_credentials_are_profile_scoped_versioned_and_never_store_plaintext() -> None:
    store, settings = _credentials_store()
    password = "correct horse battery staple"

    store.set_credentials("  Operator.ONE ", password)

    raw = cast(str, settings.values[SettingsKey.REMOTE_CONTROL_CREDENTIALS])
    assert password not in raw
    assert '"version":1' in raw
    assert '"algorithm":"scrypt"' in raw
    assert store.configured_username() == "operator.one"
    assert store.has_credentials() is True
    assert store.authenticate("OPERATOR.ONE", password) is True
    assert store.authenticate("operator.one", "incorrect password value") is False
    assert store.authenticate("different.user", password) is False


def test_credentials_fail_closed_when_record_is_corrupt_and_can_be_cleared() -> None:
    store, settings = _credentials_store()
    settings.values[SettingsKey.REMOTE_CONTROL_CREDENTIALS] = (
        '{"version":1,"username":"operator","password_envelope":{"algorithm":"scrypt"}}'
    )

    assert store.has_credentials() is False
    assert store.configured_username() == ""
    assert store.authenticate("operator", "correct horse battery staple") is False

    store.set_credentials("operator", "correct horse battery staple")
    store.clear_credentials()

    assert SettingsKey.REMOTE_CONTROL_CREDENTIALS not in settings.values
    assert store.authenticate("operator", "correct horse battery staple") is False


def test_session_store_does_not_extend_idle_deadline_on_reads_and_checks_csrf() -> None:
    clock = _Clock()
    tokens = iter(("session-one", "csrf-one"))
    sessions = InMemorySessionStore(
        absolute_ttl_seconds=25,
        idle_ttl_seconds=10,
        clock=clock,
        token_factory=lambda: next(tokens),
    )
    credentials = sessions.create("operator")

    assert credentials.session_token == "session-one"
    assert credentials.csrf_token == "csrf-one"
    clock.value = 9
    snapshot = sessions.resolve(credentials.session_token)
    assert snapshot is not None
    assert snapshot.last_activity_at == 0
    assert sessions.csrf_token(credentials.session_token) == credentials.csrf_token
    snapshot_after_bootstrap = sessions.resolve(credentials.session_token)
    assert snapshot_after_bootstrap is not None
    assert snapshot_after_bootstrap.last_activity_at == 0
    assert sessions.authorize_mutation(credentials.session_token, "wrong-token") is None

    touched = sessions.authorize_mutation(
        credentials.session_token,
        credentials.csrf_token,
    )
    assert touched is not None
    assert touched.last_activity_at == 9

    clock.value = 18.99
    assert sessions.resolve(credentials.session_token) is not None
    clock.value = 19
    assert sessions.resolve(credentials.session_token) is None
    assert sessions.csrf_token(credentials.session_token) is None
    assert sessions.active_count() == 0


def test_session_absolute_expiry_wins_over_recent_activity() -> None:
    clock = _Clock()
    tokens = iter(("session-one", "csrf-one"))
    sessions = InMemorySessionStore(
        absolute_ttl_seconds=20,
        idle_ttl_seconds=15,
        clock=clock,
        token_factory=lambda: next(tokens),
    )
    credentials = sessions.create("operator")

    clock.value = 14
    assert sessions.touch(credentials.session_token) is not None
    clock.value = 20

    assert sessions.resolve(credentials.session_token) is None


def test_session_limit_revoke_and_revoke_all_are_deterministic() -> None:
    tokens = iter(
        (
            "session-one",
            "csrf-one",
            "session-two",
            "csrf-two",
            "session-three",
            "csrf-three",
        )
    )
    sessions = InMemorySessionStore(max_sessions=2, token_factory=lambda: next(tokens))
    first = sessions.create("operator")
    second = sessions.create("operator")

    with pytest.raises(SessionLimitReachedError):
        sessions.create("operator")

    assert sessions.revoke(first.session_token) is True
    assert sessions.revoke(first.session_token) is False
    sessions.create("operator")
    assert sessions.active_count() == 2
    assert sessions.revoke_all() == 2
    assert sessions.resolve(second.session_token) is None


def test_session_inventory_uses_non_secret_management_ids_and_sanitized_metadata() -> None:
    tokens = iter(("bearer-secret", "csrf-secret"))
    monotonic = _Clock(10.0)
    wall = _Clock(1_721_130_400.0)
    sessions = InMemorySessionStore(
        clock=monotonic,
        wall_clock=wall,
        token_factory=lambda: next(tokens),
        management_id_factory=lambda: "management-public",
    )

    credentials = sessions.create(
        "operator",
        browser="Chrome\nInjected",
        platform="Android",
        client_mode="standalone",
        remote_address="192.168.1.45",
    )
    snapshot = sessions.active_sessions()[0]

    assert credentials.management_id == "management-public"
    assert len({credentials.management_id, credentials.session_token, credentials.csrf_token}) == 3
    assert snapshot.management_id == credentials.management_id
    assert snapshot.browser == "ChromeInjected"
    assert snapshot.platform == "Android"
    assert snapshot.client_mode == "standalone"
    assert snapshot.remote_address == "192.168.1.45"
    assert snapshot.created_at_utc == wall.value
    assert snapshot.last_activity_at_utc == wall.value
    assert not hasattr(snapshot, "session_token")
    assert not hasattr(snapshot, "csrf_token")

    generation = sessions.generation
    assert sessions.revoke_management_id(credentials.management_id) == (credentials.session_token)
    assert sessions.generation == generation
    assert sessions.active_sessions() == ()


def test_revoke_all_invalidates_session_creation_reserved_by_inflight_login() -> None:
    tokens = iter(("session-one", "csrf-one"))
    sessions = InMemorySessionStore(token_factory=lambda: next(tokens))
    login_generation = sessions.generation

    assert sessions.revoke_all() == 0

    with pytest.raises(SessionGenerationChangedError):
        sessions.create("operator", expected_generation=login_generation)

    assert sessions.active_count() == 0


def test_login_rate_limiter_combines_identity_and_global_sliding_windows() -> None:
    clock = _Clock()
    limiter = LoginRateLimiter(
        LoginRateLimitPolicy(per_identity_limit=2, global_limit=3, window_seconds=10),
        clock=clock,
    )

    assert limiter.attempt("192.168.1.20", " Operator ").allowed is True
    assert limiter.attempt("192.168.1.20", "operator").allowed is True
    denied_identity = limiter.attempt("192.168.1.20", "OPERATOR")
    assert denied_identity.allowed is False
    assert denied_identity.retry_after_seconds == 10

    assert limiter.attempt("192.168.1.21", "operator").allowed is True
    denied_global = limiter.attempt("192.168.1.22", "operator")
    assert denied_global.allowed is False
    assert denied_global.retry_after_seconds == 10

    clock.value = 10
    renewed = limiter.attempt("192.168.1.20", "operator")
    assert renewed.allowed is True
    assert renewed.remaining == 1


def test_login_rate_limiter_can_reset_one_identity_without_resetting_global_limit() -> None:
    limiter = LoginRateLimiter(LoginRateLimitPolicy(per_identity_limit=1, global_limit=3))

    assert limiter.attempt("192.168.1.20", "operator").allowed is True
    assert limiter.attempt("192.168.1.20", "operator").allowed is False
    limiter.reset_identity("192.168.1.20", "OPERATOR")
    assert limiter.attempt("192.168.1.20", "operator").allowed is True
    assert limiter.attempt("192.168.1.21", "operator").allowed is True
    assert limiter.attempt("192.168.1.22", "operator").allowed is False


def test_remote_control_settings_are_profile_scoped_disabled_by_default_and_fixed_port() -> None:
    settings = _MemorySettings()
    profile_settings = ProfileAppSettingsStore(cast(Any, settings))
    store = RemoteControlSettingsStore.create(profile_settings)

    assert REMOTE_CONTROL_PORT == 8765
    assert store.snapshot().enabled is False
    assert store.snapshot().network_selection is None
    assert store.onboarding_seen() is False

    store.set_enabled(True)

    assert store.enabled() is True
    assert settings.values == {SettingsKey.REMOTE_CONTROL_ENABLED: True}
    assert all("port" not in key for key in settings.values)

    store.mark_onboarding_seen()
    assert store.onboarding_seen() is True


def test_remote_control_network_selection_is_atomic_versioned_and_validated() -> None:
    settings = _MemorySettings()
    profile_settings = ProfileAppSettingsStore(cast(Any, settings))
    store = RemoteControlSettingsStore.create(profile_settings)

    store.set_network_selection("  {ADAPTER-GUID}  ", "192.168.50.12")

    selection = store.network_selection()
    assert selection is not None
    assert selection.interface_identifier == "{ADAPTER-GUID}"
    assert str(selection.ipv4_address) == "192.168.50.12"
    assert selection.selection_key == "{ADAPTER-GUID}|192.168.50.12"
    raw = cast(str, settings.values[SettingsKey.REMOTE_CONTROL_NETWORK_SELECTION])
    assert '"version":1' in raw
    assert len(settings.values) == 1

    with pytest.raises(ValueError):
        store.set_network_selection("adapter", "8.8.8.8")
    assert store.network_selection() == selection

    store.clear_network_selection()
    assert store.network_selection() is None


def test_remote_control_network_selection_fails_closed_when_persisted_value_is_invalid() -> None:
    settings = _MemorySettings()
    profile_settings = ProfileAppSettingsStore(cast(Any, settings))
    store = RemoteControlSettingsStore.create(profile_settings)
    settings.values[SettingsKey.REMOTE_CONTROL_NETWORK_SELECTION] = (
        '{"version":1,"interface_identifier":"adapter","ipv4_address":"127.0.0.1"}'
    )

    assert store.network_selection() is None
