from __future__ import annotations

import json

import pytest

from solin.core.scenes.ptz import PtzCredentialError, PtzCredentials
from solin.core.scenes.ptz_credentials import SystemPtzCredentialVault


class _Backend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


def _vault(backend: _Backend | None = None) -> tuple[SystemPtzCredentialVault, _Backend]:
    current = backend or _Backend()
    return (
        SystemPtzCredentialVault(
            "profile-one",
            current,
            reference_factory=lambda: "ptz-0123456789abcdef0123456789abcdef",
        ),
        current,
    )


def test_credentials_round_trip_only_through_opaque_keyring_envelope() -> None:
    vault, backend = _vault()
    credentials = PtzCredentials("operator", "camera-secret")

    reference = vault.save(credentials)

    assert reference == "ptz-0123456789abcdef0123456789abcdef"
    assert vault.resolve(reference) == credentials
    ((service, stored_reference), envelope) = next(iter(backend.values.items()))
    assert service.startswith("Solin.PTZ.")
    assert "profile-one" not in service
    assert stored_reference == reference
    assert json.loads(envelope) == {
        "schema_version": 1,
        "username": "operator",
        "password": "camera-secret",
    }
    assert "camera-secret" not in repr(credentials)


def test_delete_is_idempotent_and_removes_keyring_entry() -> None:
    vault, _backend = _vault()
    reference = vault.save(PtzCredentials("operator", "camera-secret"))

    vault.delete(reference)
    vault.delete(reference)

    assert vault.resolve(reference) is None


@pytest.mark.parametrize(
    "envelope",
    [
        "not-json",
        '{"schema_version":1,"username":"a","username":"b","password":"c"}',
        '{"schema_version":1,"username":"operator"}',
        '{"schema_version":1,"username":"","password":"secret"}',
    ],
)
def test_corrupt_credential_envelopes_fail_closed(envelope: str) -> None:
    vault, backend = _vault()
    reference = "ptz-0123456789abcdef0123456789abcdef"
    service = vault._service
    backend.values[(service, reference)] = envelope

    with pytest.raises(PtzCredentialError, match="ptz_credentials_corrupt"):
        vault.resolve(reference)


def test_unknown_schema_fails_closed() -> None:
    vault, backend = _vault()
    reference = "ptz-0123456789abcdef0123456789abcdef"
    backend.values[(vault._service, reference)] = json.dumps(
        {"schema_version": 2, "username": "operator", "password": "secret"}
    )

    with pytest.raises(PtzCredentialError, match="ptz_credentials_schema_unsupported"):
        vault.resolve(reference)


def test_invalid_reference_never_reaches_keyring() -> None:
    vault, backend = _vault()

    with pytest.raises(PtzCredentialError, match="ptz_credential_reference_invalid"):
        vault.resolve("../../plaintext-secret")

    assert not backend.values
