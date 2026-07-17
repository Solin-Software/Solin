from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, ip_address
import json
import os
from pathlib import Path
import threading
from typing import Final

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from solin.core.storage.binary_files import write_bytes_atomic


PRIVATE_LAN_NETWORKS: Final = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
DEFAULT_CERTIFICATE_VALIDITY: Final = timedelta(days=90)
DEFAULT_RENEW_BEFORE: Final = timedelta(days=30)
DEFAULT_ISSUER_VALIDITY: Final = timedelta(days=3 * 365)
DEFAULT_ISSUER_RENEW_BEFORE: Final = timedelta(days=180)
DEFAULT_ROOT_VALIDITY: Final = timedelta(days=10 * 365)
DEFAULT_ROOT_WARNING_BEFORE: Final = timedelta(days=365)
_CLOCK_SKEW_TOLERANCE: Final = timedelta(minutes=5)
_MANIFEST_VERSION: Final = 3


class TLSProvisioningState(StrEnum):
    HEALTHY = "healthy"
    LEAF_RENEWED = "leaf_renewed"
    ISSUER_ROTATED = "issuer_rotated"
    ROOT_CREATED = "root_created"
    ROOT_ROTATION_REQUIRED = "root_rotation_required"


class TrustAnchorRotationRequiredError(RuntimeError):
    """Raised when an installed Root CA cannot be recovered safely."""


def parse_private_lan_ipv4(value: str | IPv4Address) -> IPv4Address:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise ValueError("A valid private LAN IPv4 address is required") from error
    if not isinstance(address, IPv4Address) or not any(
        address in network for network in PRIVATE_LAN_NETWORKS
    ):
        raise ValueError("A private RFC 1918 IPv4 address is required")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError("The address must identify a private LAN interface")
    return address


@dataclass(frozen=True, slots=True)
class TLSIdentity:
    certificate_path: Path
    private_key_path: Path
    root_certificate_path: Path
    issuer_certificate_path: Path
    ipv4_address: IPv4Address
    installation_id: str
    leaf_fingerprint_sha256: str
    trust_anchor_fingerprint_sha256: str
    leaf_not_valid_after: datetime
    renew_at: datetime
    root_not_valid_after: datetime
    provisioning_state: TLSProvisioningState

    def trust_certificate_der(self) -> bytes:
        certificate = x509.load_pem_x509_certificate(self.root_certificate_path.read_bytes())
        return certificate.public_bytes(serialization.Encoding.DER)


@dataclass(frozen=True, slots=True)
class _Manifest:
    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                {"version": _MANIFEST_VERSION},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )


class TLSCertificateStore:
    """Persist one installation-local trust hierarchy and an IP-specific TLS leaf."""

    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        validity: timedelta = DEFAULT_CERTIFICATE_VALIDITY,
        renew_before: timedelta = DEFAULT_RENEW_BEFORE,
        issuer_validity: timedelta = DEFAULT_ISSUER_VALIDITY,
        issuer_renew_before: timedelta = DEFAULT_ISSUER_RENEW_BEFORE,
        root_validity: timedelta = DEFAULT_ROOT_VALIDITY,
        root_warning_before: timedelta = DEFAULT_ROOT_WARNING_BEFORE,
    ) -> None:
        if validity <= timedelta(0):
            raise ValueError("Certificate validity must be positive")
        if renew_before < timedelta(0) or renew_before >= validity:
            raise ValueError("Certificate renewal window must be below validity")
        if issuer_validity <= validity:
            raise ValueError("Issuer validity must exceed leaf validity")
        if issuer_renew_before < timedelta(0) or issuer_renew_before >= issuer_validity:
            raise ValueError("Issuer renewal window must be below validity")
        if root_validity <= issuer_validity:
            raise ValueError("Root validity must exceed issuer validity")
        if root_warning_before < timedelta(0) or root_warning_before >= root_validity:
            raise ValueError("Root warning window must be below validity")

        self._directory = Path(directory).resolve()
        self._manifest_path = self._directory / "remote-control-tls.json"
        self._lock_path = self._directory / ".remote-control-tls.lock"
        self._root_certificate_path = self._directory / "remote-control-root-cert.pem"
        self._root_backup_path = self._directory / "remote-control-root-cert.backup.pem"
        self._root_private_key_path = self._directory / "remote-control-root-key.pem"
        self._root_private_key_backup_path = self._directory / "remote-control-root-key.backup.pem"
        self._issuer_certificate_path = self._directory / "remote-control-issuer-cert.pem"
        self._issuer_private_key_path = self._directory / "remote-control-issuer-key.pem"
        self._leaf_certificate_path = self._directory / "remote-control-leaf-cert.pem"
        self._chain_path = self._directory / "remote-control-chain.pem"
        self._leaf_private_key_path = self._directory / "remote-control-leaf-key.pem"
        self._legacy_authority_certificate_path = (
            self._directory / "remote-control-authority-cert.pem"
        )
        self._legacy_authority_private_key_path = (
            self._directory / "remote-control-authority-key.pem"
        )
        self._legacy_leaf_certificate_path = self._directory / "remote-control-cert.pem"
        self._legacy_leaf_private_key_path = self._directory / "remote-control-key.pem"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._validity = validity
        self._renew_before = renew_before
        self._issuer_validity = issuer_validity
        self._issuer_renew_before = issuer_renew_before
        self._root_validity = root_validity
        self._root_warning_before = root_warning_before
        self._thread_lock = threading.Lock()

    @property
    def certificate_path(self) -> Path:
        return self._chain_path

    @property
    def private_key_path(self) -> Path:
        return self._leaf_private_key_path

    @property
    def root_certificate_path(self) -> Path:
        return self._root_certificate_path

    def load_or_create(self, ipv4_address: str | IPv4Address) -> TLSIdentity:
        address = parse_private_lan_ipv4(ipv4_address)
        with self._thread_lock:
            self._prepare_directory()
            with self._interprocess_lock():
                return self._load_or_create_locked(address)

    def renewal_due(self, identity: TLSIdentity) -> bool:
        return self._now() >= identity.renew_at

    def _load_or_create_locked(self, address: IPv4Address) -> TLSIdentity:
        now = self._now()
        hierarchy_artifacts_present = any(
            path.exists()
            for path in (
                self._manifest_path,
                self._root_certificate_path,
                self._root_backup_path,
                self._root_private_key_path,
                self._root_private_key_backup_path,
                self._issuer_certificate_path,
                self._issuer_private_key_path,
                self._leaf_certificate_path,
                self._leaf_private_key_path,
                self._chain_path,
            )
        )
        manifest = self._load_manifest()
        if manifest is None:
            manifest = _Manifest()

        root_key = self._load_private_key(self._root_private_key_path)
        if root_key is None:
            root_key = self._load_private_key(self._root_private_key_backup_path)
            if root_key is not None:
                self._write_private_key(self._root_private_key_path, root_key)
        root_certificate = self._load_certificate(self._root_certificate_path)
        if root_certificate is None:
            backup = self._load_certificate(self._root_backup_path)
            if backup is not None:
                root_certificate = backup
                self._write_certificate(self._root_certificate_path, backup)

        root_created = False
        if root_certificate is None and root_key is None and not hierarchy_artifacts_present:
            root_key = ec.generate_private_key(ec.SECP256R1())
            installation_id = _installation_id(root_key.public_key())
            root_certificate = self._issue_root(root_key, installation_id, now)
            self._write_private_key(self._root_private_key_path, root_key)
            self._write_private_key(self._root_private_key_backup_path, root_key)
            self._write_certificate(self._root_certificate_path, root_certificate)
            self._write_certificate(self._root_backup_path, root_certificate)
            root_created = True
        elif root_certificate is None or root_key is None:
            raise TrustAnchorRotationRequiredError(
                "The installed Solin Remote Root CA cannot be recovered safely"
            )
        else:
            root_public_key = root_certificate.public_key()
            if not isinstance(root_public_key, ec.EllipticCurvePublicKey):
                raise TrustAnchorRotationRequiredError(
                    "The installed Solin Remote Root CA is not supported"
                )
            installation_id = _installation_id(root_public_key)
            if not self._is_root_usable(
                root_certificate,
                root_key,
                installation_id,
                now,
            ):
                raise TrustAnchorRotationRequiredError(
                    "The installed Solin Remote Root CA cannot be recovered safely"
                )
            backup_key = self._load_private_key(self._root_private_key_backup_path)
            if backup_key is None or _public_key_bytes(
                backup_key.public_key()
            ) != _public_key_bytes(root_key.public_key()):
                self._write_private_key(self._root_private_key_backup_path, root_key)
            backup_certificate = self._load_certificate(self._root_backup_path)
            if backup_certificate is None or backup_certificate.fingerprint(
                hashes.SHA256()
            ) != root_certificate.fingerprint(hashes.SHA256()):
                self._write_certificate(self._root_backup_path, root_certificate)

        installation_id = _installation_id(root_key.public_key())

        issuer_key = self._load_private_key(self._issuer_private_key_path)
        issuer_certificate = self._load_certificate(self._issuer_certificate_path)
        issuer_rotated = (
            issuer_key is None
            or issuer_certificate is None
            or not self._is_issuer_usable(
                issuer_certificate,
                issuer_key,
                root_certificate,
                installation_id,
                now,
            )
        )
        if issuer_rotated:
            issuer_key = ec.generate_private_key(ec.SECP256R1())
            issuer_certificate = self._issue_issuer(
                issuer_key,
                root_key,
                root_certificate,
                installation_id,
                now,
            )
            self._write_private_key(self._issuer_private_key_path, issuer_key)
            self._write_certificate(self._issuer_certificate_path, issuer_certificate)
        assert issuer_key is not None
        assert issuer_certificate is not None

        leaf_key = self._load_private_key(self._leaf_private_key_path)
        leaf_certificate = self._load_certificate(self._leaf_certificate_path)
        leaf_renewed = (
            leaf_key is None
            or leaf_certificate is None
            or not self._is_leaf_usable(
                leaf_certificate,
                leaf_key,
                issuer_certificate,
                address,
                now,
            )
        )
        if leaf_renewed:
            leaf_key = ec.generate_private_key(ec.SECP256R1())
            leaf_certificate = self._issue_leaf(
                leaf_key,
                issuer_key,
                issuer_certificate,
                address,
                now,
            )
            self._write_private_key(self._leaf_private_key_path, leaf_key)
            self._write_certificate(self._leaf_certificate_path, leaf_certificate)
        assert leaf_key is not None
        assert leaf_certificate is not None

        self._write_chain(leaf_certificate, issuer_certificate)
        self._write_manifest(manifest)
        if any(
            path.exists()
            for path in (
                self._legacy_authority_certificate_path,
                self._legacy_authority_private_key_path,
                self._legacy_leaf_certificate_path,
                self._legacy_leaf_private_key_path,
            )
        ):
            self._remove_legacy_files()

        state = TLSProvisioningState.HEALTHY
        if root_created:
            state = TLSProvisioningState.ROOT_CREATED
        elif issuer_rotated:
            state = TLSProvisioningState.ISSUER_ROTATED
        elif leaf_renewed:
            state = TLSProvisioningState.LEAF_RENEWED
        if root_certificate.not_valid_after_utc - now <= self._root_warning_before:
            state = TLSProvisioningState.ROOT_ROTATION_REQUIRED

        return TLSIdentity(
            certificate_path=self._chain_path,
            private_key_path=self._leaf_private_key_path,
            root_certificate_path=self._root_certificate_path,
            issuer_certificate_path=self._issuer_certificate_path,
            ipv4_address=address,
            installation_id=installation_id,
            leaf_fingerprint_sha256=_format_fingerprint(leaf_certificate),
            trust_anchor_fingerprint_sha256=_format_fingerprint(root_certificate),
            leaf_not_valid_after=leaf_certificate.not_valid_after_utc,
            renew_at=leaf_certificate.not_valid_after_utc - self._renew_before,
            root_not_valid_after=root_certificate.not_valid_after_utc,
            provisioning_state=state,
        )

    def _prepare_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self._directory.chmod(0o700)

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _load_manifest(self) -> _Manifest | None:
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != _MANIFEST_VERSION:
            return None
        return _Manifest()

    def _write_manifest(self, manifest: _Manifest) -> None:
        write_bytes_atomic(self._manifest_path, manifest.to_bytes(), mode=0o600)

    @staticmethod
    def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey | None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return None
        try:
            raw = path.read_bytes()
            key = serialization.load_pem_private_key(raw, password=None)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            return None
        if os.name != "nt":
            path.chmod(0o600)
        return key

    @staticmethod
    def _load_certificate(path: Path) -> x509.Certificate | None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return None
        try:
            return x509.load_pem_x509_certificate(path.read_bytes())
        except (OSError, ValueError):
            return None

    def _is_root_usable(
        self,
        certificate: x509.Certificate,
        private_key: ec.EllipticCurvePrivateKey,
        installation_id: str,
        now: datetime,
    ) -> bool:
        if certificate.not_valid_before_utc > now or certificate.not_valid_after_utc <= now:
            return False
        try:
            basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
            usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            return False
        if basic != x509.BasicConstraints(ca=True, path_length=1):
            return False
        if not usage.key_cert_sign or usage.crl_sign or usage.digital_signature:
            return False
        subject = _root_subject(installation_id)
        if certificate.subject != subject or certificate.issuer != subject:
            return False
        public_key = certificate.public_key()
        return (
            isinstance(public_key, ec.EllipticCurvePublicKey)
            and _public_key_bytes(public_key) == _public_key_bytes(private_key.public_key())
            and _signature_is_valid(certificate, public_key)
        )

    def _is_issuer_usable(
        self,
        certificate: x509.Certificate,
        private_key: ec.EllipticCurvePrivateKey,
        root_certificate: x509.Certificate,
        installation_id: str,
        now: datetime,
    ) -> bool:
        if certificate.not_valid_before_utc > now:
            return False
        if certificate.not_valid_after_utc - now <= self._issuer_renew_before:
            return False
        try:
            basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
            usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
            constraints = certificate.extensions.get_extension_for_class(x509.NameConstraints).value
            eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            return False
        if basic != x509.BasicConstraints(ca=True, path_length=0):
            return False
        if not usage.key_cert_sign or usage.crl_sign or usage.digital_signature:
            return False
        if constraints != _name_constraints(installation_id):
            return False
        if eku != x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]):
            return False
        if certificate.subject != _issuer_subject(installation_id):
            return False
        if certificate.issuer != root_certificate.subject:
            return False
        public_key = certificate.public_key()
        root_public_key = root_certificate.public_key()
        return (
            isinstance(public_key, ec.EllipticCurvePublicKey)
            and isinstance(root_public_key, ec.EllipticCurvePublicKey)
            and _public_key_bytes(public_key) == _public_key_bytes(private_key.public_key())
            and _signature_is_valid(certificate, root_public_key)
        )

    def _is_leaf_usable(
        self,
        certificate: x509.Certificate,
        private_key: ec.EllipticCurvePrivateKey,
        issuer_certificate: x509.Certificate,
        address: IPv4Address,
        now: datetime,
    ) -> bool:
        if certificate.not_valid_before_utc > now:
            return False
        if certificate.not_valid_after_utc - now <= self._renew_before:
            return False
        try:
            san_extension = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            )
            basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
            usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
            eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            return False
        if san_extension.critical:
            return False
        if san_extension.value.get_values_for_type(x509.IPAddress) != [address]:
            return False
        if len(san_extension.value) != 1:
            return False
        if certificate.subject != _leaf_subject():
            return False
        if basic != x509.BasicConstraints(ca=False, path_length=None):
            return False
        if not usage.digital_signature or usage.key_cert_sign or usage.crl_sign:
            return False
        if eku != x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]):
            return False
        if certificate.issuer != issuer_certificate.subject:
            return False
        public_key = certificate.public_key()
        issuer_public_key = issuer_certificate.public_key()
        return (
            isinstance(public_key, ec.EllipticCurvePublicKey)
            and isinstance(issuer_public_key, ec.EllipticCurvePublicKey)
            and _public_key_bytes(public_key) == _public_key_bytes(private_key.public_key())
            and _signature_is_valid(certificate, issuer_public_key)
        )

    def _issue_root(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        installation_id: str,
        now: datetime,
    ) -> x509.Certificate:
        subject = _root_subject(installation_id)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW_TOLERANCE)
            .not_valid_after(now + self._root_validity)
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(_ca_key_usage(), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(private_key.public_key()),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )

    def _issue_issuer(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        root_key: ec.EllipticCurvePrivateKey,
        root_certificate: x509.Certificate,
        installation_id: str,
        now: datetime,
    ) -> x509.Certificate:
        not_valid_after = min(now + self._issuer_validity, root_certificate.not_valid_after_utc)
        return (
            x509.CertificateBuilder()
            .subject_name(_issuer_subject(installation_id))
            .issuer_name(root_certificate.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW_TOLERANCE)
            .not_valid_after(not_valid_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(_name_constraints(installation_id), critical=True)
            .add_extension(_ca_key_usage(), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                critical=False,
            )
            .sign(root_key, hashes.SHA256())
        )

    def _issue_leaf(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        issuer_key: ec.EllipticCurvePrivateKey,
        issuer_certificate: x509.Certificate,
        address: IPv4Address,
        now: datetime,
    ) -> x509.Certificate:
        not_valid_after = min(now + self._validity, issuer_certificate.not_valid_after_utc)
        return (
            x509.CertificateBuilder()
            .subject_name(_leaf_subject())
            .issuer_name(issuer_certificate.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW_TOLERANCE)
            .not_valid_after(not_valid_after)
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
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                critical=False,
            )
            .sign(issuer_key, hashes.SHA256())
        )

    @staticmethod
    def _write_private_key(path: Path, private_key: ec.EllipticCurvePrivateKey) -> None:
        write_bytes_atomic(
            path,
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            mode=0o600,
        )

    @staticmethod
    def _write_certificate(path: Path, certificate: x509.Certificate) -> None:
        write_bytes_atomic(
            path,
            certificate.public_bytes(serialization.Encoding.PEM),
            mode=0o644,
        )

    def _write_chain(self, leaf: x509.Certificate, issuer: x509.Certificate) -> None:
        write_bytes_atomic(
            self._chain_path,
            leaf.public_bytes(serialization.Encoding.PEM)
            + issuer.public_bytes(serialization.Encoding.PEM),
            mode=0o644,
        )

    def _remove_legacy_files(self) -> None:
        for path in (
            self._legacy_authority_certificate_path,
            self._legacy_authority_private_key_path,
            self._legacy_leaf_certificate_path,
            self._legacy_leaf_private_key_path,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Certificate clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def verification_code(fingerprint: str) -> str:
    compact = "".join(
        character for character in fingerprint.upper() if character in "0123456789ABCDEF"
    )
    if len(compact) < 16:
        return ""
    return " · ".join(compact[index : index + 4] for index in range(0, 16, 4))


def _installation_id(public_key: ec.EllipticCurvePublicKey) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(_public_key_bytes(public_key))
    return digest.finalize()[:16].hex()


def _root_subject(installation_id: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solin"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Local Remote Control"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"Solin Remote Root {installation_id[:12]}"),
        ]
    )


def _issuer_subject(installation_id: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solin"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Local Remote Control"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"Solin Remote Issuer {installation_id[:12]}"),
        ]
    )


def _leaf_subject() -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solin"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Solin Remote Control"),
        ]
    )


def _name_constraints(installation_id: str) -> x509.NameConstraints:
    return x509.NameConstraints(
        permitted_subtrees=[
            *(x509.IPAddress(network) for network in PRIVATE_LAN_NETWORKS),
            x509.DNSName(f".{installation_id}.remote.solin.invalid"),
        ],
        excluded_subtrees=None,
    )


def _ca_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
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
