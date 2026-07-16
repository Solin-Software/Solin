from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network, ip_address
import os
from pathlib import Path
import tempfile
import threading
from typing import Final

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


_PRIVATE_LAN_NETWORKS: Final = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
DEFAULT_CERTIFICATE_VALIDITY: Final = timedelta(days=397)
DEFAULT_RENEW_BEFORE: Final = timedelta(days=30)
DEFAULT_AUTHORITY_VALIDITY: Final = timedelta(days=3650)
DEFAULT_AUTHORITY_RENEW_BEFORE: Final = timedelta(days=180)
_CLOCK_SKEW_TOLERANCE: Final = timedelta(minutes=5)


def parse_private_lan_ipv4(value: str | IPv4Address) -> IPv4Address:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise ValueError("A valid private LAN IPv4 address is required") from error
    if not isinstance(address, IPv4Address) or not any(
        address in network for network in _PRIVATE_LAN_NETWORKS
    ):
        raise ValueError("A private RFC 1918 IPv4 address is required")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError("The address must identify a private LAN interface")
    return address


@dataclass(frozen=True, slots=True)
class TLSIdentity:
    certificate_path: Path
    private_key_path: Path
    authority_certificate_path: Path
    ipv4_address: IPv4Address
    fingerprint_sha256: str
    authority_fingerprint_sha256: str
    not_valid_after: datetime

    def authority_certificate_der(self) -> bytes:
        certificate = x509.load_pem_x509_certificate(self.authority_certificate_path.read_bytes())
        return certificate.public_bytes(serialization.Encoding.DER)


class TLSCertificateStore:
    """Persist an IP-scoped local authority and its HTTPS server identity."""

    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        validity: timedelta = DEFAULT_CERTIFICATE_VALIDITY,
        renew_before: timedelta = DEFAULT_RENEW_BEFORE,
        authority_validity: timedelta = DEFAULT_AUTHORITY_VALIDITY,
        authority_renew_before: timedelta = DEFAULT_AUTHORITY_RENEW_BEFORE,
    ) -> None:
        if validity <= timedelta(0):
            raise ValueError("Certificate validity must be positive")
        if renew_before < timedelta(0) or renew_before >= validity:
            raise ValueError("Certificate renewal window must be non-negative and below validity")
        if authority_validity <= timedelta(0):
            raise ValueError("Authority validity must be positive")
        if authority_validity <= validity:
            raise ValueError("Authority validity must exceed server certificate validity")
        if authority_renew_before < timedelta(0) or authority_renew_before >= authority_validity:
            raise ValueError("Authority renewal window must be non-negative and below validity")
        self._directory = Path(directory).resolve()
        self._certificate_path = self._directory / "remote-control-cert.pem"
        self._private_key_path = self._directory / "remote-control-key.pem"
        self._authority_certificate_path = self._directory / "remote-control-authority-cert.pem"
        self._authority_private_key_path = self._directory / "remote-control-authority-key.pem"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._validity = validity
        self._renew_before = renew_before
        self._authority_validity = authority_validity
        self._authority_renew_before = authority_renew_before
        self._lock = threading.Lock()

    @property
    def certificate_path(self) -> Path:
        return self._certificate_path

    @property
    def private_key_path(self) -> Path:
        return self._private_key_path

    @property
    def authority_certificate_path(self) -> Path:
        return self._authority_certificate_path

    def load_or_create(self, ipv4_address: str | IPv4Address) -> TLSIdentity:
        address = parse_private_lan_ipv4(ipv4_address)
        with self._lock:
            now = self._now()
            self._prepare_directory()
            authority_private_key = self._load_private_key(self._authority_private_key_path)
            authority_certificate = self._load_certificate(self._authority_certificate_path)
            authority_changed = (
                authority_private_key is None
                or authority_certificate is None
                or not self._is_authority_usable(
                    authority_certificate,
                    authority_private_key,
                    address,
                    now,
                )
            )
            if authority_changed:
                authority_private_key = ec.generate_private_key(ec.SECP256R1())
                authority_certificate = self._issue_authority(
                    authority_private_key,
                    address,
                    now,
                )
                self._write_private_key(
                    self._authority_private_key_path,
                    authority_private_key,
                )
                self._write_certificate(
                    self._authority_certificate_path,
                    authority_certificate,
                )
            assert authority_private_key is not None
            assert authority_certificate is not None

            private_key = self._load_private_key(self._private_key_path)
            if private_key is None:
                private_key = ec.generate_private_key(ec.SECP256R1())
                self._write_private_key(self._private_key_path, private_key)

            certificate = self._load_certificate(self._certificate_path)
            if certificate is None or not self._is_usable(
                certificate,
                private_key,
                authority_certificate,
                address,
                now,
            ):
                certificate = self._issue(
                    private_key,
                    authority_private_key,
                    authority_certificate,
                    address,
                    now,
                )
                self._write_certificate(self._certificate_path, certificate)

            return TLSIdentity(
                certificate_path=self._certificate_path,
                private_key_path=self._private_key_path,
                authority_certificate_path=self._authority_certificate_path,
                ipv4_address=address,
                fingerprint_sha256=_format_fingerprint(certificate),
                authority_fingerprint_sha256=_format_fingerprint(authority_certificate),
                not_valid_after=certificate.not_valid_after_utc,
            )

    def _prepare_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self._directory.chmod(0o700)

    @staticmethod
    def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey | None:
        if path.is_symlink():
            return None
        try:
            raw = path.read_bytes()
            key = serialization.load_pem_private_key(raw, password=None)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve,
            ec.SECP256R1,
        ):
            return None
        if os.name != "nt":
            path.chmod(0o600)
        return key

    @staticmethod
    def _load_certificate(path: Path) -> x509.Certificate | None:
        if path.is_symlink():
            return None
        try:
            return x509.load_pem_x509_certificate(path.read_bytes())
        except (OSError, ValueError):
            return None

    def _is_authority_usable(
        self,
        certificate: x509.Certificate,
        private_key: ec.EllipticCurvePrivateKey,
        address: IPv4Address,
        now: datetime,
    ) -> bool:
        if certificate.not_valid_before_utc > now:
            return False
        authority_renewal_window = max(
            self._authority_renew_before,
            self._validity + _CLOCK_SKEW_TOLERANCE,
        )
        if certificate.not_valid_after_utc - now <= authority_renewal_window:
            return False
        try:
            basic_constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
            name_constraints = certificate.extensions.get_extension_for_class(
                x509.NameConstraints
            ).value
        except x509.ExtensionNotFound:
            return False
        expected_scope = IPv4Network(f"{address}/32")
        if basic_constraints != x509.BasicConstraints(ca=True, path_length=0):
            return False
        if not key_usage.key_cert_sign or not key_usage.crl_sign or key_usage.digital_signature:
            return False
        if name_constraints.permitted_subtrees != [x509.IPAddress(expected_scope)]:
            return False
        if name_constraints.excluded_subtrees is not None:
            return False
        subject = _authority_subject(address)
        if certificate.subject != subject or certificate.issuer != subject:
            return False
        certificate_public_key = certificate.public_key()
        if not isinstance(certificate_public_key, ec.EllipticCurvePublicKey):
            return False
        if _public_key_bytes(certificate_public_key) != _public_key_bytes(private_key.public_key()):
            return False
        return _signature_is_valid(certificate, certificate_public_key)

    def _is_usable(
        self,
        certificate: x509.Certificate,
        private_key: ec.EllipticCurvePrivateKey,
        authority_certificate: x509.Certificate,
        address: IPv4Address,
        now: datetime,
    ) -> bool:
        if certificate.not_valid_before_utc > now:
            return False
        if certificate.not_valid_after_utc - now <= self._renew_before:
            return False
        try:
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            addresses = san.get_values_for_type(x509.IPAddress)
            basic_constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            extended_key_usage = certificate.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage
            ).value
            key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            return False
        if addresses != [address]:
            return False
        if basic_constraints.ca or ExtendedKeyUsageOID.SERVER_AUTH not in extended_key_usage:
            return False
        if not key_usage.digital_signature or key_usage.key_cert_sign or key_usage.crl_sign:
            return False
        if certificate.subject != _certificate_subject():
            return False
        if certificate.issuer != authority_certificate.subject:
            return False

        certificate_public_key = certificate.public_key()
        if not isinstance(certificate_public_key, ec.EllipticCurvePublicKey):
            return False
        if _public_key_bytes(certificate_public_key) != _public_key_bytes(private_key.public_key()):
            return False
        authority_public_key = authority_certificate.public_key()
        if not isinstance(authority_public_key, ec.EllipticCurvePublicKey):
            return False
        if not _signature_is_valid(certificate, authority_public_key):
            return False
        return True

    def _issue_authority(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        address: IPv4Address,
        now: datetime,
    ) -> x509.Certificate:
        subject = _authority_subject(address)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW_TOLERANCE)
            .not_valid_after(now + self._authority_validity)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.NameConstraints(
                    permitted_subtrees=[x509.IPAddress(IPv4Network(f"{address}/32"))],
                    excluded_subtrees=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )

    def _issue(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        authority_private_key: ec.EllipticCurvePrivateKey,
        authority_certificate: x509.Certificate,
        address: IPv4Address,
        now: datetime,
    ) -> x509.Certificate:
        subject = _certificate_subject()
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(authority_certificate.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW_TOLERANCE)
            .not_valid_after(now + self._validity)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    authority_private_key.public_key()
                ),
                critical=False,
            )
            .sign(authority_private_key, hashes.SHA256())
        )

    @staticmethod
    def _write_private_key(
        path: Path,
        private_key: ec.EllipticCurvePrivateKey,
    ) -> None:
        raw = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        _atomic_write(path, raw, mode=0o600)

    @staticmethod
    def _write_certificate(path: Path, certificate: x509.Certificate) -> None:
        _atomic_write(
            path,
            certificate.public_bytes(serialization.Encoding.PEM),
            mode=0o644,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Certificate clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _certificate_subject() -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solin"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Solin Remote Control"),
        ]
    )


def _authority_subject(address: IPv4Address) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solin"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Solin Remote Control CA ({address})",
            ),
        ]
    )


def _signature_is_valid(
    certificate: x509.Certificate,
    public_key: ec.EllipticCurvePublicKey,
) -> bool:
    signature_hash_algorithm = certificate.signature_hash_algorithm
    if signature_hash_algorithm is None:
        return False
    try:
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(signature_hash_algorithm),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def _format_fingerprint(certificate: x509.Certificate) -> str:
    raw = certificate.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass  # Best effort only after the atomic replace/write path has already failed.
