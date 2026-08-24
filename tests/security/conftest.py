"""Throwaway PKI for tests/security's live-handshake tests.

Deliberately not reused from src/ or i3-fe-core/: this fixture exists only to
give the handshake tests something to present, not to validate certificate
*construction*, which is i3-fe-core's own concern (security/peer_auth.py,
tested there).
"""
from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _write_pem(path: Path, pem_bytes: bytes) -> Path:
    path.write_bytes(pem_bytes)
    return path


def _self_signed_ca(common_name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf_cert(
    common_name: str,
    signing_key,
    signing_cert,
    *,
    san_dns: list[str] | None = None,
    san_ip: list[str] | None = None,
):
    """A leaf cert signed by (signing_key, signing_cert). Pass signing_key ==
    its own freshly generated key and signing_cert == None to self-sign
    (used for the untrusted client cert — same shape as a real cert, chains
    to nothing this server trusts)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = signing_cert.subject if signing_cert is not None else subject
    now = datetime.datetime.now(datetime.timezone.utc)
    san_entries: list[x509.GeneralName] = [x509.DNSName(d) for d in (san_dns or [])]
    san_entries += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in (san_ip or [])]
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if san_entries:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
    signer_key = signing_key if signing_cert is not None else key
    cert = builder.sign(signer_key, hashes.SHA256())
    return key, cert


def _key_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@dataclass(frozen=True)
class TestPKI:
    """Paths to a throwaway CA + server identity + two client identities."""

    ca_cert: Path
    server_cert: Path
    server_key: Path
    trusted_client_cert: Path
    trusted_client_key: Path
    untrusted_client_cert: Path
    untrusted_client_key: Path


@pytest.fixture(scope="session")
def tls_pki(tmp_path_factory: pytest.TempPathFactory) -> TestPKI:
    certs_dir = tmp_path_factory.mktemp("lvf_test_certs")

    ca_key, ca_cert = _self_signed_ca("LVF Test CA — throwaway, not for reuse")

    server_key, server_cert = _leaf_cert(
        "localhost",
        ca_key,
        ca_cert,
        san_dns=["localhost"],
        san_ip=["127.0.0.1"],
    )

    trusted_client_key, trusted_client_cert = _leaf_cert(
        "lvf-test-client-trusted",
        ca_key,
        ca_cert,
    )

    # Self-signed — NOT issued by ca_cert. Structurally a valid certificate;
    # chains to nothing this server's CA bundle trusts.
    untrusted_client_key, untrusted_client_cert = _leaf_cert(
        "lvf-test-client-untrusted",
        None,
        None,
    )

    return TestPKI(
        ca_cert=_write_pem(certs_dir / "ca.crt", _cert_pem(ca_cert)),
        server_cert=_write_pem(certs_dir / "server.crt", _cert_pem(server_cert)),
        server_key=_write_pem(certs_dir / "server.key", _key_pem(server_key)),
        trusted_client_cert=_write_pem(
            certs_dir / "client_trusted.crt", _cert_pem(trusted_client_cert)
        ),
        trusted_client_key=_write_pem(
            certs_dir / "client_trusted.key", _key_pem(trusted_client_key)
        ),
        untrusted_client_cert=_write_pem(
            certs_dir / "client_untrusted.crt", _cert_pem(untrusted_client_cert)
        ),
        untrusted_client_key=_write_pem(
            certs_dir / "client_untrusted.key", _key_pem(untrusted_client_key)
        ),
    )
