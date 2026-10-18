"""Server identity for SNP/1: a self-signed ECDSA P-256 certificate and its SPKI pin (spec section 5.1).

The key and certificate live in ``<data_dir>/snp/`` (directory 0700, files 0600). The pin is
the SHA-256 of the certificate's SubjectPublicKeyInfo, so it survives certificate renewals
that keep the key. The certificate is only re-issued when it is close to expiry or when its
subject alternative names no longer match the configuration.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger("sarness.snp")

VALIDITY_DAYS = 3650
RENEW_BEFORE_DAYS = 30
WILDCARD_HOSTS = ("", "0.0.0.0", "::")


@dataclass(frozen=True)
class Identity:
    cert_path: Path
    key_path: Path
    cert_der: bytes
    spki_sha256: bytes

    @property
    def pin(self) -> str:
        return "sha256:" + self.spki_sha256.hex()

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_alpn_protocols(["snp/1"])
        ctx.num_tickets = 0
        ctx.load_cert_chain(str(self.cert_path), str(self.key_path))
        return ctx


def spki_sha256_of_der(cert_der: bytes) -> bytes:
    """SHA-256 over the DER SubjectPublicKeyInfo of a DER certificate (the SNP pin)."""
    cert = x509.load_der_x509_certificate(cert_der)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).digest()


def san_names(host: str | None, extra: list[str] | None) -> list[str]:
    """Names and addresses the certificate should cover, de-duplicated, in a stable order."""
    names = ["localhost", "127.0.0.1", "::1"]
    if host and host not in WILDCARD_HOSTS:
        names.append(host)
    names += [n.strip() for n in (extra or []) if n and n.strip()]
    seen, out = set(), []
    for n in names:
        try:
            n = str(ipaddress.ip_address(n))
        except ValueError:
            n = n.lower()
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _general_names(names: list[str]) -> list[x509.GeneralName]:
    out: list[x509.GeneralName] = []
    for n in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            out.append(x509.DNSName(n))
    return out


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _issue(key: ec.EllipticCurvePrivateKey, names: list[str]) -> x509.Certificate:
    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sarness Engine SNP")])
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(hours=1))
            .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
            .add_extension(x509.SubjectAlternativeName(_general_names(names)), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))


def _cert_names(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
           [str(v) for v in san.get_values_for_type(x509.IPAddress)]


def load_or_create(data_dir: Path, host: str | None = None, extra_names: list[str] | None = None) -> Identity:
    snp_dir = Path(data_dir) / "snp"
    snp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snp_dir, 0o700)
    key_path, cert_path = snp_dir / "server.key", snp_dir / "server.crt"
    names = san_names(host, extra_names)

    key = None
    if key_path.exists():
        try:
            loaded = serialization.load_pem_private_key(key_path.read_bytes(), None)
            if isinstance(loaded, ec.EllipticCurvePrivateKey) and loaded.curve.name == "secp256r1":
                key = loaded
        except (ValueError, TypeError):
            log.error("SNP key %s is unreadable; generating a new identity", key_path)
    if key is None:
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        cert_path.unlink(missing_ok=True)
    os.chmod(key_path, 0o600)

    cert = None
    if cert_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except ValueError:
            cert = None
    if cert is not None:
        same_key = cert.public_key().public_numbers() == key.public_key().public_numbers()
        left = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        if not same_key or left < dt.timedelta(days=RENEW_BEFORE_DAYS) or set(_cert_names(cert)) != set(names):
            cert = None
    if cert is None:
        cert = _issue(key, names)
        _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        log.info("issued SNP certificate for %s", ", ".join(names))
    os.chmod(cert_path, 0o600)

    der = cert.public_bytes(serialization.Encoding.DER)
    return Identity(cert_path, key_path, der, spki_sha256_of_der(der))
