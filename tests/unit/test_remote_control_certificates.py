from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from solin.core.remote_control.certificates import (
    PRIVATE_LAN_NETWORKS,
    TLSCertificateStore,
    TLSProvisioningState,
    TrustAnchorRotationRequiredError,
    parse_private_lan_ipv4,
    verification_code,
)
from solin.core.remote_control.server import RemoteControlServerBinding


@dataclass(slots=True)
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return key


def _public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _assert_signed_by(certificate: x509.Certificate, issuer: x509.Certificate) -> None:
    issuer_key = issuer.public_key()
    assert isinstance(issuer_key, ec.EllipticCurvePublicKey)
    issuer_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        ec.ECDSA(certificate.signature_hash_algorithm),
    )


def test_certificate_store_creates_constrained_hierarchy_and_server_chain(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path / "tls", clock=lambda: now)

    identity = store.load_or_create("192.168.10.25")
    chain = x509.load_pem_x509_certificates(identity.certificate_path.read_bytes())
    assert len(chain) == 2
    leaf, issuer = chain
    root = _certificate(identity.root_certificate_path)
    private_key = _private_key(identity.private_key_path)

    leaf_san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert leaf_san.critical is False
    assert list(leaf_san.value) == [x509.IPAddress(identity.ipv4_address)]
    leaf_key = leaf.public_key()
    assert isinstance(leaf_key, ec.EllipticCurvePublicKey)
    assert _public_key_bytes(leaf_key) == _public_key_bytes(private_key.public_key())
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value == (
        x509.BasicConstraints(ca=False, path_length=None)
    )
    assert leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == (
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
    )

    issuer_constraints = issuer.extensions.get_extension_for_class(x509.NameConstraints)
    assert issuer_constraints.critical is True
    assert issuer_constraints.value.permitted_subtrees == [
        *(x509.IPAddress(network) for network in PRIVATE_LAN_NETWORKS),
        x509.DNSName(f".{identity.installation_id}.remote.solin.invalid"),
    ]
    assert issuer.extensions.get_extension_for_class(x509.BasicConstraints).value == (
        x509.BasicConstraints(ca=True, path_length=0)
    )
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value == (
        x509.BasicConstraints(ca=True, path_length=1)
    )
    with pytest.raises(x509.ExtensionNotFound):
        root.extensions.get_extension_for_class(x509.NameConstraints)

    _assert_signed_by(leaf, issuer)
    _assert_signed_by(issuer, root)
    _assert_signed_by(root, root)
    assert root.subject == root.issuer
    assert identity.leaf_not_valid_after == now + timedelta(days=90)
    assert identity.renew_at == now + timedelta(days=60)
    assert identity.provisioning_state is TLSProvisioningState.ROOT_CREATED
    assert re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", identity.leaf_fingerprint_sha256)
    assert re.fullmatch(
        r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}",
        identity.trust_anchor_fingerprint_sha256,
    )
    assert identity.trust_certificate_der() == root.public_bytes(serialization.Encoding.DER)
    assert identity.root_certificate_path.read_bytes() not in identity.certificate_path.read_bytes()


def test_certificate_store_reuses_a_healthy_hierarchy(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)

    first = store.load_or_create("10.1.2.3")
    original = {
        path.name: path.read_bytes()
        for path in (
            first.certificate_path,
            first.private_key_path,
            first.root_certificate_path,
            first.issuer_certificate_path,
        )
    }
    second = store.load_or_create("10.1.2.3")

    assert second.installation_id == first.installation_id
    assert second.leaf_fingerprint_sha256 == first.leaf_fingerprint_sha256
    assert second.trust_anchor_fingerprint_sha256 == first.trust_anchor_fingerprint_sha256
    assert second.provisioning_state is TLSProvisioningState.HEALTHY
    assert all(
        path.read_bytes() == original[path.name]
        for path in (
            second.certificate_path,
            second.private_key_path,
            second.root_certificate_path,
            second.issuer_certificate_path,
        )
    )


def test_ip_change_rotates_leaf_and_key_but_preserves_installed_authority(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)

    first = store.load_or_create("172.16.1.2")
    root_bytes = first.root_certificate_path.read_bytes()
    issuer_bytes = first.issuer_certificate_path.read_bytes()
    leaf_key_bytes = first.private_key_path.read_bytes()

    second = store.load_or_create("172.16.1.3")
    leaf = x509.load_pem_x509_certificates(second.certificate_path.read_bytes())[0]

    assert second.installation_id == first.installation_id
    assert second.trust_anchor_fingerprint_sha256 == first.trust_anchor_fingerprint_sha256
    assert second.leaf_fingerprint_sha256 != first.leaf_fingerprint_sha256
    assert second.root_certificate_path.read_bytes() == root_bytes
    assert second.issuer_certificate_path.read_bytes() == issuer_bytes
    assert second.private_key_path.read_bytes() != leaf_key_bytes
    assert list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) == [
        x509.IPAddress(second.ipv4_address)
    ]


def test_leaf_renews_at_deadline_without_rotating_root_or_issuer(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 7, 16, 12, tzinfo=UTC))
    store = TLSCertificateStore(tmp_path, clock=clock)
    first = store.load_or_create("192.168.1.5")
    root_bytes = first.root_certificate_path.read_bytes()
    issuer_bytes = first.issuer_certificate_path.read_bytes()
    leaf_key_bytes = first.private_key_path.read_bytes()

    clock.value = first.renew_at - timedelta(seconds=1)
    assert store.renewal_due(first) is False
    unchanged = store.load_or_create("192.168.1.5")
    assert unchanged.leaf_fingerprint_sha256 == first.leaf_fingerprint_sha256

    clock.value = first.renew_at
    assert store.renewal_due(first) is True
    renewed = store.load_or_create("192.168.1.5")
    assert renewed.leaf_fingerprint_sha256 != first.leaf_fingerprint_sha256
    assert renewed.private_key_path.read_bytes() != leaf_key_bytes
    assert renewed.root_certificate_path.read_bytes() == root_bytes
    assert renewed.issuer_certificate_path.read_bytes() == issuer_bytes


def test_corrupt_leaf_rotates_only_leaf_material(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    first = store.load_or_create("192.168.1.5")
    root_bytes = first.root_certificate_path.read_bytes()
    issuer_bytes = first.issuer_certificate_path.read_bytes()

    first.private_key_path.write_bytes(b"not a private key")
    recovered = store.load_or_create("192.168.1.5")

    assert recovered.leaf_fingerprint_sha256 != first.leaf_fingerprint_sha256
    assert recovered.root_certificate_path.read_bytes() == root_bytes
    assert recovered.issuer_certificate_path.read_bytes() == issuer_bytes
    leaf = x509.load_pem_x509_certificates(recovered.certificate_path.read_bytes())[0]
    leaf_key = _private_key(recovered.private_key_path)
    assert isinstance(leaf.public_key(), ec.EllipticCurvePublicKey)
    assert _public_key_bytes(leaf.public_key()) == _public_key_bytes(leaf_key.public_key())


def test_corrupt_issuer_rotates_issuer_and_leaf_but_preserves_root(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    first = store.load_or_create("192.168.1.5")
    root_bytes = first.root_certificate_path.read_bytes()
    issuer_bytes = first.issuer_certificate_path.read_bytes()

    first.issuer_certificate_path.write_bytes(b"not a certificate")
    recovered = store.load_or_create("192.168.1.5")

    assert recovered.root_certificate_path.read_bytes() == root_bytes
    assert recovered.issuer_certificate_path.read_bytes() != issuer_bytes
    assert recovered.leaf_fingerprint_sha256 != first.leaf_fingerprint_sha256


def test_root_backups_recover_corrupt_primary_without_changing_trust(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    first = store.load_or_create("192.168.1.5")
    root_bytes = first.root_certificate_path.read_bytes()
    fingerprint = first.trust_anchor_fingerprint_sha256

    first.root_certificate_path.write_bytes(b"not a certificate")
    (tmp_path / "remote-control-root-key.pem").write_bytes(b"not a private key")
    recovered = store.load_or_create("192.168.1.5")

    assert recovered.root_certificate_path.read_bytes() == root_bytes
    assert recovered.trust_anchor_fingerprint_sha256 == fingerprint


def test_unrecoverable_root_fails_closed_instead_of_silently_rotating(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    identity = store.load_or_create("192.168.1.5")

    for path in (
        identity.root_certificate_path,
        tmp_path / "remote-control-root-cert.backup.pem",
        tmp_path / "remote-control-root-key.pem",
        tmp_path / "remote-control-root-key.backup.pem",
    ):
        path.write_bytes(b"corrupt")

    with pytest.raises(TrustAnchorRotationRequiredError):
        store.load_or_create("192.168.1.5")


def test_pre_release_scoped_authority_artifacts_are_replaced_without_user_migration_state(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    legacy_key = ec.generate_private_key(ec.SECP256R1())
    legacy_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Legacy Solin")])
    legacy = (
        x509.CertificateBuilder()
        .subject_name(legacy_subject)
        .issuer_name(legacy_subject)
        .public_key(legacy_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=397))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.IPAddress(IPv4Network("192.168.1.5/32"))],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .sign(legacy_key, hashes.SHA256())
    )
    legacy_path = tmp_path / "remote-control-authority-cert.pem"
    legacy_path.write_bytes(legacy.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "remote-control-authority-key.pem").write_bytes(
        legacy_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    migrated = store.load_or_create("192.168.1.5")

    assert migrated.provisioning_state is TLSProvisioningState.ROOT_CREATED
    assert not legacy_path.exists()
    assert not (tmp_path / "remote-control-authority-key.pem").exists()

    assert store.load_or_create("192.168.1.5").provisioning_state is (TLSProvisioningState.HEALTHY)


@pytest.mark.parametrize(
    "address",
    ("127.0.0.1", "169.254.1.1", "8.8.8.8", "::1", "not-an-address"),
)
def test_private_lan_address_rejects_non_rfc1918_addresses(address: str) -> None:
    with pytest.raises(ValueError):
        parse_private_lan_ipv4(address)


def test_certificate_clock_requires_timezone_awareness(tmp_path: Path) -> None:
    store = TLSCertificateStore(tmp_path, clock=lambda: datetime(2026, 7, 16, 12))

    with pytest.raises(ValueError, match="timezone-aware"):
        store.load_or_create("192.168.1.5")


def test_verification_code_uses_a_short_human_comparable_prefix() -> None:
    fingerprint = ":".join(f"{value:02X}" for value in range(32))
    assert verification_code(fingerprint) == "0001 · 0203 · 0405 · 0607"
    assert verification_code("invalid") == ""


def test_server_binding_requires_the_certificate_ip_and_fixed_port(tmp_path: Path) -> None:
    identity = TLSCertificateStore(tmp_path).load_or_create("192.168.1.5")

    binding = RemoteControlServerBinding("192.168.1.5", 8765, identity)

    assert binding.url == "https://192.168.1.5:8765/remote/"
    with pytest.raises(ValueError, match="certificate IPv4"):
        RemoteControlServerBinding("192.168.1.6", 8765, identity)
    with pytest.raises(ValueError, match="fixed port"):
        RemoteControlServerBinding("192.168.1.5", 9876, identity)
