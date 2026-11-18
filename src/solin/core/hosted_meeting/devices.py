"""Approved stage and moderator devices (Qt-free).

The first time a stage device or a remote moderator opens their page, they give
their name and the meeting password, and the operator approves them. After that the
device is remembered (for :data:`APPROVAL_LIFETIME`) and skips both steps:

* a **moderator** device proves itself with a key pair its browser created at
  approval (WebCrypto ECDSA P-256, private key non-extractable, kept in
  IndexedDB): every visit it signs a fresh challenge, so a copied cookie, link or
  password is useless on another device. On a page that is not a secure context
  (plain HTTP on the hall network) WebCrypto is unavailable, and the device falls
  back to a random secret like the stage page;
* a **stage** device keeps a random secret in the browser; the page is read-only.

Approved devices belong to the congregation, not to one link, so they keep working
when each meeting has its own link. "New link" removes them all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

APPROVAL_LIFETIME_S = 183 * 24 * 3600   # about six months
MAX_DEVICES = 100
CHALLENGE_PREFIX = b"solin-meeting-device:"


class StaffRole(StrEnum):
    STAGE = "stage"
    MODERATOR = "moderator"


class KeyKind(StrEnum):
    ECDSA = "ecdsa"     # P-256 public key (SPKI, base64)
    SECRET = "secret"   # sha256 of a random secret


@dataclass(frozen=True, slots=True)
class ApprovedDevice:
    device_id: str
    role: StaffRole
    name: str
    key_kind: KeyKind
    key: str                 # base64 SPKI (ecdsa) or hex sha256 (secret)
    approved_at: float
    last_seen: float = 0.0
    last_network: str = ""

    def expired(self, now: float) -> bool:
        return now - self.approved_at > APPROVAL_LIFETIME_S

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = self.role.value
        data["key_kind"] = self.key_kind.value
        return data

    @classmethod
    def from_json(cls, data: Any) -> ApprovedDevice | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(device_id=str(data["device_id"]), role=StaffRole(data["role"]), name=str(data["name"]),
                       key_kind=KeyKind(data["key_kind"]), key=str(data["key"]),
                       approved_at=float(data["approved_at"]), last_seen=float(data.get("last_seen") or 0),
                       last_network=str(data.get("last_network") or ""))
        except (KeyError, ValueError, TypeError):
            return None


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def load_public_key(spki_b64: str) -> ec.EllipticCurvePublicKey | None:
    """A P-256 public key from the browser's SPKI export, or None if it is anything else."""

    try:
        key = serialization.load_der_public_key(base64.b64decode(spki_b64, validate=True))
    except (ValueError, TypeError):
        return None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        return None
    return key


def new_challenge() -> str:
    return secrets.token_urlsafe(24)


def challenge_message(challenge: str) -> bytes:
    return CHALLENGE_PREFIX + challenge.encode("ascii")


def verify_signature(spki_b64: str, challenge: str, signature_b64: str) -> bool:
    """WebCrypto ECDSA signatures are raw r||s (64 bytes); cryptography wants DER."""

    key = load_public_key(spki_b64)
    if key is None:
        return False
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, challenge_message(challenge), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def network_of(address: str) -> str:
    """The network an address belongs to (IPv4 /24, IPv6 /64), for "new network" notes."""

    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


class DeviceRegistry:
    """The approved devices; the meeting runtime owns it and reports every change."""

    def __init__(self, devices: list[ApprovedDevice] | None = None) -> None:
        self._devices: dict[str, ApprovedDevice] = {d.device_id: d for d in devices or ()}

    @classmethod
    def from_json(cls, data: Any) -> DeviceRegistry:
        items = data if isinstance(data, list) else []
        return cls([d for d in (ApprovedDevice.from_json(item) for item in items) if d is not None])

    def to_json(self) -> list[dict[str, Any]]:
        return [d.to_json() for d in self._devices.values()]

    def all(self, now: float) -> list[ApprovedDevice]:
        return sorted((d for d in self._devices.values() if not d.expired(now)), key=lambda d: d.name.casefold())

    def get(self, device_id: str, now: float) -> ApprovedDevice | None:
        device = self._devices.get(device_id)
        if device is None or device.expired(now):
            return None
        return device

    def approve(self, device: ApprovedDevice) -> None:
        self._devices[device.device_id] = device
        if len(self._devices) > MAX_DEVICES:
            oldest = min(self._devices.values(), key=lambda d: max(d.last_seen, d.approved_at))
            self._devices.pop(oldest.device_id, None)

    def remove(self, device_id: str) -> bool:
        return self._devices.pop(device_id, None) is not None

    def clear(self) -> None:
        self._devices.clear()

    def prune(self, now: float) -> bool:
        expired = [d.device_id for d in self._devices.values() if d.expired(now)]
        for device_id in expired:
            del self._devices[device_id]
        return bool(expired)

    def check_secret(self, device_id: str, secret: str, now: float) -> ApprovedDevice | None:
        device = self.get(device_id, now)
        if device is None or device.key_kind is not KeyKind.SECRET:
            return None
        return device if hmac.compare_digest(device.key, secret_hash(secret)) else None

    def check_signature(self, device_id: str, challenge: str, signature: str, now: float) -> ApprovedDevice | None:
        device = self.get(device_id, now)
        if device is None or device.key_kind is not KeyKind.ECDSA:
            return None
        return device if verify_signature(device.key, challenge, signature) else None

    def seen(self, device_id: str, network: str, now: float) -> bool:
        """Record a visit; True when it came from a different network than the last one."""

        device = self._devices.get(device_id)
        if device is None:
            return False
        changed = bool(device.last_network and network and network != device.last_network)
        self._devices[device_id] = replace(device, last_seen=now, last_network=network or device.last_network)
        return changed
