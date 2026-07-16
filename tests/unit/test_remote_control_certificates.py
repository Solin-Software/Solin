from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from solin.core.remote_control.certificates import (
    TLSCertificateStore,
    parse_private_lan_ipv4,
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


def test_certificate_store_creates_tls_identity_with_exact_ipv4_san(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path / "tls", clock=lambda: now)

    identity = store.load_or_create("192.168.10.25")
    certificate = _certificate(identity.certificate_path)
    authority = _certificate(identity.authority_certificate_path)
    private_key = _private_key(identity.private_key_path)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    name_constraints = authority.extensions.get_extension_for_class(x509.NameConstraints).value
    basic_constraints = authority.extensions.get_extension_for_class(x509.BasicConstraints).value

    assert san.get_values_for_type(x509.IPAddress) == [identity.ipv4_address]
    certificate_key = certificate.public_key()
    assert isinstance(certificate_key, ec.EllipticCurvePublicKey)
    assert _public_key_bytes(certificate_key) == _public_key_bytes(private_key.public_key())
    authority_key = authority.public_key()
    assert isinstance(authority_key, ec.EllipticCurvePublicKey)
    authority_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        ec.ECDSA(certificate.signature_hash_algorithm),
    )
    assert certificate.issuer == authority.subject
    assert authority.issuer == authority.subject
    assert basic_constraints == x509.BasicConstraints(ca=True, path_length=0)
    assert name_constraints.permitted_subtrees == [
        x509.IPAddress(IPv4Network(f"{identity.ipv4_address}/32"))
    ]
    assert identity.not_valid_after == now + timedelta(days=397)
    assert re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", identity.fingerprint_sha256)
    assert re.fullmatch(
        r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}",
        identity.authority_fingerprint_sha256,
    )
    assert identity.authority_certificate_der() == authority.public_bytes(
        serialization.Encoding.DER
    )
    assert authority.fingerprint(hashes.SHA256()) != certificate.fingerprint(hashes.SHA256())
    assert b"PRIVATE KEY" in identity.private_key_path.read_bytes()
    assert b"CERTIFICATE" in identity.certificate_path.read_bytes()


def test_certificate_store_reuses_valid_certificate_and_installation_key(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)

    first = store.load_or_create("10.1.2.3")
    certificate_bytes = first.certificate_path.read_bytes()
    key_bytes = first.private_key_path.read_bytes()
    second = store.load_or_create("10.1.2.3")

    assert second.fingerprint_sha256 == first.fingerprint_sha256
    assert second.certificate_path.read_bytes() == certificate_bytes
    assert second.private_key_path.read_bytes() == key_bytes


def test_ip_change_reissues_certificate_without_rotating_installation_key(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)

    first = store.load_or_create("172.16.1.2")
    key_bytes = first.private_key_path.read_bytes()
    authority_key_bytes = (tmp_path / "remote-control-authority-key.pem").read_bytes()
    second = store.load_or_create("172.16.1.3")
    san = (
        _certificate(second.certificate_path)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )

    assert second.fingerprint_sha256 != first.fingerprint_sha256
    assert second.authority_fingerprint_sha256 != first.authority_fingerprint_sha256
    assert second.private_key_path.read_bytes() == key_bytes
    assert (tmp_path / "remote-control-authority-key.pem").read_bytes() != authority_key_bytes
    assert san.get_values_for_type(x509.IPAddress) == [second.ipv4_address]


def test_certificate_renews_before_expiry_and_recovers_corrupt_certificate(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 7, 16, 12, tzinfo=UTC))
    store = TLSCertificateStore(tmp_path, clock=clock)
    first = store.load_or_create("192.168.1.5")
    key_bytes = first.private_key_path.read_bytes()

    clock.value += timedelta(days=368)
    renewed = store.load_or_create("192.168.1.5")
    assert renewed.fingerprint_sha256 != first.fingerprint_sha256
    assert renewed.private_key_path.read_bytes() == key_bytes

    renewed.certificate_path.write_bytes(b"not a certificate")
    recovered = store.load_or_create("192.168.1.5")
    assert recovered.certificate_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert recovered.private_key_path.read_bytes() == key_bytes


def test_corrupt_private_key_rotates_key_and_reissues_matching_certificate(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    first = store.load_or_create("192.168.1.5")
    original_key = first.private_key_path.read_bytes()

    first.private_key_path.write_bytes(b"not a private key")
    recovered = store.load_or_create("192.168.1.5")
    recovered_key = _private_key(recovered.private_key_path)
    certificate_key = _certificate(recovered.certificate_path).public_key()

    assert recovered.private_key_path.read_bytes() != original_key
    assert recovered.fingerprint_sha256 != first.fingerprint_sha256
    assert isinstance(certificate_key, ec.EllipticCurvePublicKey)
    assert _public_key_bytes(certificate_key) == _public_key_bytes(recovered_key.public_key())


def test_corrupt_authority_rotates_only_the_scoped_authority_and_reissues_leaf(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    store = TLSCertificateStore(tmp_path, clock=lambda: now)
    first = store.load_or_create("192.168.1.5")
    leaf_key = first.private_key_path.read_bytes()
    authority_key_path = tmp_path / "remote-control-authority-key.pem"
    original_authority_key = authority_key_path.read_bytes()

    first.authority_certificate_path.write_bytes(b"not a certificate")
    recovered = store.load_or_create("192.168.1.5")

    assert recovered.authority_fingerprint_sha256 != first.authority_fingerprint_sha256
    assert recovered.fingerprint_sha256 != first.fingerprint_sha256
    assert recovered.private_key_path.read_bytes() == leaf_key
    assert authority_key_path.read_bytes() != original_authority_key


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


def test_server_binding_requires_the_certificate_ip_and_fixed_port(tmp_path: Path) -> None:
    identity = TLSCertificateStore(tmp_path).load_or_create("192.168.1.5")

    binding = RemoteControlServerBinding("192.168.1.5", 8765, identity)

    assert binding.url == "https://192.168.1.5:8765/remote/"
    with pytest.raises(ValueError, match="certificate IPv4"):
        RemoteControlServerBinding("192.168.1.6", 8765, identity)
    with pytest.raises(ValueError, match="fixed port"):
        RemoteControlServerBinding("192.168.1.5", 9876, identity)
