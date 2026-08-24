"""Profile-scoped PTZ credentials backed only by an OS-protected keyring."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
import re
import sys
import threading
from typing import Protocol, cast
from uuid import uuid4

from keyring.errors import KeyringError

from solin.core.scenes.ptz import (
    PtzCredentialError,
    PtzCredentials,
)


_CREDENTIAL_SCHEMA_VERSION = 1
_CREDENTIAL_REFERENCE_PATTERN = re.compile(r"^ptz-[0-9a-f]{32}$")
_MAXIMUM_ENVELOPE_BYTES = 8192


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class UnavailablePtzCredentialVault:
    def __init__(self, error_code: str) -> None:
        self._error_code = error_code

    def save(self, credentials: PtzCredentials) -> str:
        del credentials
        raise PtzCredentialError(self._error_code)

    def resolve(self, credential_ref: str) -> PtzCredentials | None:
        del credential_ref
        raise PtzCredentialError(self._error_code)

    def delete(self, credential_ref: str) -> None:
        del credential_ref
        raise PtzCredentialError(self._error_code)


class SystemPtzCredentialVault:
    """Stores opaque credential envelopes without any plaintext-file fallback."""

    def __init__(
        self,
        profile_id: str,
        backend: _KeyringBackend,
        *,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or len(profile_id) > 512
            or any(ord(character) < 32 for character in profile_id)
        ):
            raise ValueError("Invalid PTZ credential profile identity")
        self._service = "Solin.PTZ." + sha256(profile_id.encode("utf-8")).hexdigest()[:32]
        self._backend = backend
        self._reference_factory = reference_factory or _new_credential_reference
        self._lock = threading.RLock()

    def save(self, credentials: PtzCredentials) -> str:
        if not isinstance(credentials, PtzCredentials):
            raise TypeError("PTZ credentials are required")
        credential_ref = self._reference_factory()
        _validate_credential_reference(credential_ref)
        envelope = json.dumps(
            {
                "schema_version": _CREDENTIAL_SCHEMA_VERSION,
                "username": credentials.username,
                "password": credentials.password,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(envelope.encode("utf-8")) > _MAXIMUM_ENVELOPE_BYTES:
            raise PtzCredentialError("ptz_credentials_too_large")
        with self._lock:
            try:
                self._backend.set_password(self._service, credential_ref, envelope)
            except (KeyringError, OSError, RuntimeError) as error:
                raise PtzCredentialError("ptz_credential_store_unavailable") from error
        return credential_ref

    def resolve(self, credential_ref: str) -> PtzCredentials | None:
        _validate_credential_reference(credential_ref)
        with self._lock:
            try:
                envelope = self._backend.get_password(self._service, credential_ref)
            except (KeyringError, OSError, RuntimeError) as error:
                raise PtzCredentialError("ptz_credential_store_unavailable") from error
        if envelope is None:
            return None
        return _decode_credentials(envelope)

    def delete(self, credential_ref: str) -> None:
        _validate_credential_reference(credential_ref)
        with self._lock:
            try:
                if self._backend.get_password(self._service, credential_ref) is None:
                    return
                self._backend.delete_password(self._service, credential_ref)
            except (KeyringError, OSError, RuntimeError) as error:
                raise PtzCredentialError("ptz_credential_store_unavailable") from error


def create_system_ptz_credential_vault(
    profile_id: str,
    *,
    platform: str | None = None,
) -> SystemPtzCredentialVault:
    current_platform = platform or sys.platform
    try:
        if current_platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            backend = cast(_KeyringBackend, WinVaultKeyring())
        elif current_platform == "darwin":
            from keyring.backends.macOS import Keyring

            backend = cast(_KeyringBackend, Keyring())
        elif current_platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring

            backend = cast(_KeyringBackend, Keyring())
        else:
            raise PtzCredentialError("ptz_credential_store_unsupported")
    except PtzCredentialError:
        raise
    except (ImportError, KeyringError, OSError, RuntimeError) as error:
        raise PtzCredentialError("ptz_credential_store_unavailable") from error
    return SystemPtzCredentialVault(profile_id, backend)


def _new_credential_reference() -> str:
    return f"ptz-{uuid4().hex}"


def _validate_credential_reference(credential_ref: str) -> None:
    if not isinstance(credential_ref, str) or not _CREDENTIAL_REFERENCE_PATTERN.fullmatch(
        credential_ref
    ):
        raise PtzCredentialError("ptz_credential_reference_invalid")


def _decode_credentials(envelope: str) -> PtzCredentials:
    if not isinstance(envelope, str) or len(envelope.encode("utf-8")) > _MAXIMUM_ENVELOPE_BYTES:
        raise PtzCredentialError("ptz_credentials_corrupt")
    try:
        decoded = json.loads(envelope, object_pairs_hook=_strict_object)
    except (TypeError, ValueError, UnicodeError) as error:
        raise PtzCredentialError("ptz_credentials_corrupt") from error
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema_version",
        "username",
        "password",
    }:
        raise PtzCredentialError("ptz_credentials_corrupt")
    if decoded["schema_version"] != _CREDENTIAL_SCHEMA_VERSION:
        raise PtzCredentialError("ptz_credentials_schema_unsupported")
    try:
        return PtzCredentials(decoded["username"], decoded["password"])
    except (TypeError, ValueError) as error:
        raise PtzCredentialError("ptz_credentials_corrupt") from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result
