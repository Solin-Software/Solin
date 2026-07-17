from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
import unicodedata
from typing import Final

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import ProfileAppSettingsStore


USERNAME_MIN_LENGTH: Final = 3
USERNAME_MAX_LENGTH: Final = 64
PASSWORD_MIN_LENGTH: Final = 12
PASSWORD_MAX_LENGTH: Final = 128
DEFAULT_SESSION_ABSOLUTE_TTL_SECONDS: Final = 12 * 60 * 60
DEFAULT_SESSION_IDLE_TTL_SECONDS: Final = 2 * 60 * 60
DEFAULT_MAX_SESSIONS: Final = 8

_CREDENTIAL_RECORD_VERSION: Final = 1
_PASSWORD_ENVELOPE_VERSION: Final = 1
_USERNAME_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9._-]*", re.ASCII)


class UsernameValidationError(ValueError):
    """Raised when a remote-control username does not meet the public contract."""


class PasswordValidationError(ValueError):
    """Raised when a remote-control password does not meet the public contract."""


class SessionLimitReachedError(RuntimeError):
    """Raised when a profile already has the maximum number of active sessions."""


class SessionGenerationChangedError(RuntimeError):
    """Raised when a login attempts to create a session after global revocation."""


def normalize_username(username: str) -> str:
    """Return the canonical case-insensitive username used for storage and login."""

    if not isinstance(username, str):
        raise UsernameValidationError("Username must be text.")
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not USERNAME_MIN_LENGTH <= len(normalized) <= USERNAME_MAX_LENGTH:
        raise UsernameValidationError(
            f"Username must contain {USERNAME_MIN_LENGTH} to {USERNAME_MAX_LENGTH} characters."
        )
    if _USERNAME_PATTERN.fullmatch(normalized) is None:
        raise UsernameValidationError(
            "Username must start with a letter or number and contain only letters, numbers, "
            "periods, underscores, or hyphens."
        )
    return normalized


def validate_password(password: str) -> str:
    """Validate a password without trimming or otherwise changing its value."""

    if not isinstance(password, str):
        raise PasswordValidationError("Password must be text.")
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise PasswordValidationError(
            f"Password must contain {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters."
        )
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in password):
        raise PasswordValidationError("Password cannot contain control characters.")
    return password


@dataclass(frozen=True, slots=True)
class ScryptParameters:
    n: int = 32_768
    r: int = 8
    p: int = 1
    length: int = 32
    salt_length: int = 16
    max_memory_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.n < 2 or self.n & (self.n - 1):
            raise ValueError("scrypt n must be a power of two greater than one")
        if self.r <= 0 or self.p <= 0:
            raise ValueError("scrypt r and p must be positive")
        if not 16 <= self.length <= 64:
            raise ValueError("scrypt output length must be between 16 and 64 bytes")
        if not 16 <= self.salt_length <= 64:
            raise ValueError("scrypt salt length must be between 16 and 64 bytes")
        if self.max_memory_bytes <= 0:
            raise ValueError("scrypt maximum memory must be positive")


DEFAULT_SCRYPT_PARAMETERS: Final = ScryptParameters()


@dataclass(frozen=True, slots=True)
class PasswordEnvelope:
    version: int
    algorithm: str
    n: int
    r: int
    p: int
    length: int
    salt: bytes
    digest: bytes

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "algorithm": self.algorithm,
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "length": self.length,
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "digest": base64.b64encode(self.digest).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, value: object) -> PasswordEnvelope | None:
        if not isinstance(value, dict):
            return None
        try:
            version = int(value["version"])
            algorithm = str(value["algorithm"])
            n = int(value["n"])
            r = int(value["r"])
            p = int(value["p"])
            length = int(value["length"])
            salt = base64.b64decode(str(value["salt"]), validate=True)
            digest = base64.b64decode(str(value["digest"]), validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            return None

        if version != _PASSWORD_ENVELOPE_VERSION or algorithm != "scrypt":
            return None
        if n < 2 or n > 1_048_576 or n & (n - 1):
            return None
        if r <= 0 or r > 64 or p <= 0 or p > 16:
            return None
        if not 16 <= length <= 64 or len(digest) != length:
            return None
        if not 16 <= len(salt) <= 64:
            return None
        return cls(version, algorithm, n, r, p, length, salt, digest)


class ScryptPasswordHasher:
    """Create and verify bounded, versioned scrypt password envelopes."""

    def __init__(
        self,
        parameters: ScryptParameters = DEFAULT_SCRYPT_PARAMETERS,
        *,
        salt_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._parameters = parameters
        self._salt_factory = salt_factory
        self._dummy_envelope = self.hash_password(secrets.token_urlsafe(24))

    @property
    def parameters(self) -> ScryptParameters:
        return self._parameters

    @property
    def dummy_envelope(self) -> PasswordEnvelope:
        return self._dummy_envelope

    def hash_password(self, password: str) -> PasswordEnvelope:
        validated = validate_password(password)
        salt = self._salt_factory(self._parameters.salt_length)
        if len(salt) != self._parameters.salt_length:
            raise ValueError("Salt factory returned an unexpected number of bytes")
        digest = self._derive(validated, salt, self._parameters)
        return PasswordEnvelope(
            version=_PASSWORD_ENVELOPE_VERSION,
            algorithm="scrypt",
            n=self._parameters.n,
            r=self._parameters.r,
            p=self._parameters.p,
            length=self._parameters.length,
            salt=salt,
            digest=digest,
        )

    def verify_password(self, password: str, envelope: PasswordEnvelope) -> bool:
        try:
            validated = validate_password(password)
            parameters = ScryptParameters(
                n=envelope.n,
                r=envelope.r,
                p=envelope.p,
                length=envelope.length,
                salt_length=len(envelope.salt),
                max_memory_bytes=self._parameters.max_memory_bytes,
            )
            candidate = self._derive(validated, envelope.salt, parameters)
        except (PasswordValidationError, ValueError):
            return False
        return hmac.compare_digest(candidate, envelope.digest)

    @staticmethod
    def _derive(password: str, salt: bytes, parameters: ScryptParameters) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=parameters.n,
            r=parameters.r,
            p=parameters.p,
            maxmem=parameters.max_memory_bytes,
            dklen=parameters.length,
        )


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    username: str
    password_envelope: PasswordEnvelope

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": _CREDENTIAL_RECORD_VERSION,
                "username": self.username,
                "password_envelope": self.password_envelope.to_dict(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, raw: str) -> CredentialRecord | None:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(value, dict) or value.get("version") != _CREDENTIAL_RECORD_VERSION:
            return None
        try:
            username = normalize_username(value["username"])
        except (KeyError, UsernameValidationError, TypeError):
            return None
        envelope = PasswordEnvelope.from_dict(value.get("password_envelope"))
        if envelope is None:
            return None
        return cls(username, envelope)


@dataclass(frozen=True, slots=True)
class RemoteControlCredentialsStore:
    """Profile-scoped remote credentials stored as one fail-closed record."""

    profile_settings: ProfileAppSettingsStore
    password_hasher: ScryptPasswordHasher

    @classmethod
    def create(
        cls,
        profile_settings: ProfileAppSettingsStore,
        *,
        password_hasher: ScryptPasswordHasher | None = None,
    ) -> RemoteControlCredentialsStore:
        return cls(profile_settings, password_hasher or ScryptPasswordHasher())

    def configured_username(self) -> str:
        record = self._record()
        return record.username if record is not None else ""

    def has_credentials(self) -> bool:
        return self._record() is not None

    def set_credentials(self, username: str, password: str) -> None:
        record = CredentialRecord(
            username=normalize_username(username),
            password_envelope=self.password_hasher.hash_password(password),
        )
        self.profile_settings.settings.set_value(
            SettingsKey.REMOTE_CONTROL_CREDENTIALS,
            record.serialize(),
        )

    def authenticate(self, username: str, password: str) -> bool:
        record = self._record()
        try:
            candidate_username = normalize_username(username)
        except UsernameValidationError:
            candidate_username = ""

        expected_username = record.username if record is not None else "invalid-user"
        envelope = (
            record.password_envelope if record is not None else self.password_hasher.dummy_envelope
        )
        username_matches = _constant_time_text_equal(candidate_username, expected_username)
        password_matches = self.password_hasher.verify_password(password, envelope)
        return username_matches and password_matches and record is not None

    def clear_credentials(self) -> None:
        self.profile_settings.settings.remove(SettingsKey.REMOTE_CONTROL_CREDENTIALS)

    def _record(self) -> CredentialRecord | None:
        raw = self.profile_settings.settings.string(SettingsKey.REMOTE_CONTROL_CREDENTIALS)
        return CredentialRecord.deserialize(raw) if raw else None


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    session_token: str
    csrf_token: str
    management_id: str


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    management_id: str
    principal: str
    browser: str
    platform: str
    client_mode: str
    remote_address: str
    created_at: float
    last_activity_at: float
    created_at_utc: float
    last_activity_at_utc: float
    absolute_expires_at: float
    idle_expires_at: float


@dataclass(frozen=True, slots=True)
class RemoteSessionInfo:
    management_id: str
    principal: str
    browser: str
    platform: str
    client_mode: str
    remote_address: str
    created_at_utc: float
    last_activity_at_utc: float
    connected_socket_count: int


@dataclass(slots=True)
class _Session:
    management_id: str
    principal: str
    csrf_token: str
    browser: str
    platform: str
    client_mode: str
    remote_address: str
    created_at: float
    last_activity_at: float
    created_at_utc: float
    last_activity_at_utc: float


class InMemorySessionStore:
    """Thread-safe opaque sessions with explicit user-activity semantics."""

    def __init__(
        self,
        *,
        absolute_ttl_seconds: float = DEFAULT_SESSION_ABSOLUTE_TTL_SECONDS,
        idle_ttl_seconds: float = DEFAULT_SESSION_IDLE_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
        management_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(absolute_ttl_seconds) or absolute_ttl_seconds <= 0:
            raise ValueError("Absolute session TTL must be positive and finite")
        if not math.isfinite(idle_ttl_seconds) or idle_ttl_seconds <= 0:
            raise ValueError("Idle session TTL must be positive and finite")
        if max_sessions <= 0:
            raise ValueError("Maximum sessions must be positive")
        self._absolute_ttl_seconds = float(absolute_ttl_seconds)
        self._idle_ttl_seconds = float(idle_ttl_seconds)
        self._max_sessions = max_sessions
        self._clock = clock
        self._wall_clock = wall_clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._management_id_factory = management_id_factory or (lambda: secrets.token_urlsafe(12))
        self._sessions: dict[str, _Session] = {}
        self._generation = 0
        self._dummy_csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def create(
        self,
        principal: str,
        *,
        browser: str = "",
        platform: str = "",
        client_mode: str = "browser",
        remote_address: str = "",
        expected_generation: int | None = None,
    ) -> SessionCredentials:
        if not principal:
            raise ValueError("Session principal cannot be empty")
        with self._lock:
            now = self._now()
            self._purge_expired_locked(now)
            if expected_generation is not None and expected_generation != self._generation:
                raise SessionGenerationChangedError(
                    "Session generation changed while authentication was in progress"
                )
            if len(self._sessions) >= self._max_sessions:
                raise SessionLimitReachedError(
                    f"Maximum of {self._max_sessions} active sessions reached"
                )
            session_token = self._unique_token_locked()
            csrf_token = self._token_factory()
            if not csrf_token:
                raise ValueError("Token factory returned an empty CSRF token")
            management_id = self._unique_management_id_locked()
            now_utc = self._wall_now()
            self._sessions[session_token] = _Session(
                management_id=management_id,
                principal=principal,
                csrf_token=csrf_token,
                browser=_bounded_session_text(browser, 32),
                platform=_bounded_session_text(platform, 32),
                client_mode=("standalone" if client_mode == "standalone" else "browser"),
                remote_address=_bounded_session_text(remote_address, 64),
                created_at=now,
                last_activity_at=now,
                created_at_utc=now_utc,
                last_activity_at_utc=now_utc,
            )
            return SessionCredentials(session_token, csrf_token, management_id)

    def resolve(self, session_token: str) -> SessionSnapshot | None:
        """Authenticate a session without extending its idle deadline."""

        with self._lock:
            now = self._now()
            session = self._live_session_locked(session_token, now)
            return self._snapshot(session) if session is not None else None

    def csrf_token(self, session_token: str) -> str | None:
        """Return the bootstrap CSRF secret without extending the idle deadline."""

        with self._lock:
            session = self._live_session_locked(session_token, self._now())
            return session.csrf_token if session is not None else None

    def authorize_mutation(
        self,
        session_token: str,
        csrf_token: str,
        *,
        touch: bool = True,
    ) -> SessionSnapshot | None:
        """Validate session and CSRF tokens, optionally recording user activity."""

        with self._lock:
            now = self._now()
            session = self._live_session_locked(session_token, now)
            expected = session.csrf_token if session is not None else self._dummy_csrf_token
            csrf_matches = _constant_time_text_equal(csrf_token, expected)
            if session is None or not csrf_matches:
                return None
            if touch:
                session.last_activity_at = now
                session.last_activity_at_utc = self._wall_now()
            return self._snapshot(session)

    def touch(self, session_token: str) -> SessionSnapshot | None:
        with self._lock:
            now = self._now()
            session = self._live_session_locked(session_token, now)
            if session is None:
                return None
            session.last_activity_at = now
            session.last_activity_at_utc = self._wall_now()
            return self._snapshot(session)

    def revoke(self, session_token: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_token, None) is not None

    def revoke_management_id(self, management_id: str) -> str | None:
        """Revoke one session by its non-secret management identity."""

        with self._lock:
            token = next(
                (
                    candidate
                    for candidate, session in self._sessions.items()
                    if session.management_id == management_id
                ),
                None,
            )
            if token is not None:
                self._sessions.pop(token, None)
            return token

    def revoke_all(self) -> int:
        with self._lock:
            revoked = len(self._sessions)
            self._sessions.clear()
            self._generation += 1
            return revoked

    def active_count(self) -> int:
        with self._lock:
            self._purge_expired_locked(self._now())
            return len(self._sessions)

    def active_sessions(self) -> tuple[SessionSnapshot, ...]:
        with self._lock:
            self._purge_expired_locked(self._now())
            snapshots = [self._snapshot(session) for session in self._sessions.values()]
        return tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (
                    -snapshot.last_activity_at,
                    snapshot.management_id,
                ),
            )
        )

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked(self._now())

    def _unique_token_locked(self) -> str:
        for _attempt in range(16):
            token = self._token_factory()
            if token and token not in self._sessions:
                return token
        raise RuntimeError("Token factory failed to produce a unique non-empty session token")

    def _unique_management_id_locked(self) -> str:
        existing = {session.management_id for session in self._sessions.values()}
        for _attempt in range(16):
            management_id = self._management_id_factory()
            if management_id and management_id not in existing:
                return management_id
        raise RuntimeError("Management ID factory failed to produce a unique non-empty value")

    def _live_session_locked(self, token: str, now: float) -> _Session | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if self._is_expired(session, now):
            self._sessions.pop(token, None)
            return None
        return session

    def _purge_expired_locked(self, now: float) -> int:
        expired = [
            token for token, session in self._sessions.items() if self._is_expired(session, now)
        ]
        for token in expired:
            self._sessions.pop(token, None)
        return len(expired)

    def _is_expired(self, session: _Session, now: float) -> bool:
        return (
            now >= session.created_at + self._absolute_ttl_seconds
            or now >= session.last_activity_at + self._idle_ttl_seconds
        )

    def _snapshot(self, session: _Session) -> SessionSnapshot:
        return SessionSnapshot(
            management_id=session.management_id,
            principal=session.principal,
            browser=session.browser,
            platform=session.platform,
            client_mode=session.client_mode,
            remote_address=session.remote_address,
            created_at=session.created_at,
            last_activity_at=session.last_activity_at,
            created_at_utc=session.created_at_utc,
            last_activity_at_utc=session.last_activity_at_utc,
            absolute_expires_at=session.created_at + self._absolute_ttl_seconds,
            idle_expires_at=session.last_activity_at + self._idle_ttl_seconds,
        )

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("Session clock must return a finite value")
        return now

    def _wall_now(self) -> float:
        now = float(self._wall_clock())
        if not math.isfinite(now):
            raise ValueError("Session wall clock must return a finite value")
        return now


def _bounded_session_text(value: str, maximum: int) -> str:
    return "".join(character for character in str(value or "") if character.isprintable())[:maximum]


@dataclass(frozen=True, slots=True)
class LoginRateLimitPolicy:
    per_identity_limit: int = 5
    global_limit: int = 100
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.per_identity_limit <= 0 or self.global_limit <= 0:
            raise ValueError("Rate limits must be positive")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("Rate-limit window must be positive and finite")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class LoginRateLimiter:
    """Bounded sliding-window limiter keyed by client address and username."""

    def __init__(
        self,
        policy: LoginRateLimitPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or LoginRateLimitPolicy()
        self._clock = clock
        self._global_attempts: deque[float] = deque()
        self._identity_attempts: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def attempt(self, client_address: str, username: str) -> RateLimitDecision:
        identity = self._identity_key(client_address, username)
        with self._lock:
            now = self._now()
            self._purge_all_locked(now)
            identity_attempts = self._identity_attempts.setdefault(identity, deque())

            waits: list[float] = []
            if len(identity_attempts) >= self._policy.per_identity_limit:
                waits.append(self._retry_after(identity_attempts, now))
            if len(self._global_attempts) >= self._policy.global_limit:
                waits.append(self._retry_after(self._global_attempts, now))
            if waits:
                if not identity_attempts:
                    self._identity_attempts.pop(identity, None)
                return RateLimitDecision(False, 0, max(waits))

            identity_attempts.append(now)
            self._global_attempts.append(now)
            remaining = min(
                self._policy.per_identity_limit - len(identity_attempts),
                self._policy.global_limit - len(self._global_attempts),
            )
            return RateLimitDecision(True, remaining, 0.0)

    def reset_identity(self, client_address: str, username: str) -> None:
        with self._lock:
            self._identity_attempts.pop(self._identity_key(client_address, username), None)

    def clear(self) -> None:
        with self._lock:
            self._global_attempts.clear()
            self._identity_attempts.clear()

    def _purge_all_locked(self, now: float) -> None:
        self._purge_deque(self._global_attempts, now)
        empty: list[tuple[str, str]] = []
        for identity, attempts in self._identity_attempts.items():
            self._purge_deque(attempts, now)
            if not attempts:
                empty.append(identity)
        for identity in empty:
            self._identity_attempts.pop(identity, None)

    def _purge_deque(self, attempts: deque[float], now: float) -> None:
        cutoff = now - self._policy.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def _retry_after(self, attempts: deque[float], now: float) -> float:
        return max(0.0, attempts[0] + self._policy.window_seconds - now)

    @staticmethod
    def _identity_key(client_address: str, username: str) -> tuple[str, str]:
        address_key = str(client_address).strip().casefold() or "<unknown>"
        username_key = unicodedata.normalize("NFKC", str(username)).strip().casefold()
        return address_key, username_key or "<invalid>"

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("Rate-limit clock must return a finite value")
        return now


def _constant_time_text_equal(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
