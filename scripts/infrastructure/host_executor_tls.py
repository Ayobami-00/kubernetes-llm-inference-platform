"""Operator-local, dedicated mutual-TLS identity. Never place the CA key in the cluster."""

from datetime import UTC, datetime, timedelta

from common import private_dir, write_private
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_NAME = "host-executor.platform-ops.svc"


def pem_key(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def material(folder, *, now=None):
    """Reuse retained identities; renewal is an explicit separately staged rotation."""
    now = now or datetime.now(UTC)
    private_dir(folder)
    if not (folder / "ca.key").exists():
        if any(folder.iterdir()):
            raise ValueError("Incomplete TLS custody; never silently replace its CA")
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "klip-host-executor-ca")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        write_private(folder / "ca.key", pem_key(key))
        write_private(
            folder / "ca.crt", certificate.public_bytes(serialization.Encoding.PEM).decode()
        )
        for role, cn, usage in (
            ("server", SERVER_NAME, ExtendedKeyUsageOID.SERVER_AUTH),
            ("client", "klip-management-host-client", ExtendedKeyUsageOID.CLIENT_AUTH),
        ):
            leaf_key = ec.generate_private_key(ec.SECP256R1())
            leaf = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
                .issuer_name(certificate.subject)
                .public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
            )
            if role == "server":
                leaf = leaf.add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False
                )
            leaf = leaf.sign(key, hashes.SHA256())
            private_dir(folder / role)
            write_private(folder / role / "tls.key", pem_key(leaf_key))
            write_private(
                folder / role / "tls.crt", leaf.public_bytes(serialization.Encoding.PEM).decode()
            )
            write_private(
                folder / role / "ca.crt",
                certificate.public_bytes(serialization.Encoding.PEM).decode(),
            )
    # Refuse expired, mismatched or partially retained material, rather than rotating on a retry.
    ca = x509.load_pem_x509_certificate((folder / "ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((folder / "ca.key").read_bytes(), password=None)
    if ca.public_key() != ca_key.public_key():
        raise ValueError("Retained executor CA key differs from its certificate")
    for role, usage in (
        ("server", ExtendedKeyUsageOID.SERVER_AUTH),
        ("client", ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        leaf = x509.load_pem_x509_certificate((folder / role / "tls.crt").read_bytes())
        key = serialization.load_pem_private_key(
            (folder / role / "tls.key").read_bytes(), password=None
        )
        leaf.verify_directly_issued_by(ca)
        if (
            leaf.public_key() != key.public_key()
            or leaf.not_valid_after_utc <= now + timedelta(days=7)
            or now < leaf.not_valid_before_utc
            or list(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != [usage]
            or (folder / role / "ca.crt").read_bytes() != (folder / "ca.crt").read_bytes()
        ):
            raise ValueError("TLS identity differs or needs an explicitly staged renewal")
    return {role: folder / role for role in ("server", "client")}
