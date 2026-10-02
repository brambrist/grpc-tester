import datetime
import ipaddress

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _make_cert(cn, issuer=None, is_ca=False, sans=()):
    """Returns (cert, key). issuer is a (cert, key) pair; None means self-signed."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer_cert, issuer_key = issuer or (None, key)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_cert.subject if issuer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
    return builder.sign(issuer_key, hashes.SHA256()), key


def _cert_pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def pki(tmp_path):
    """A CA, a server cert valid for 127.0.0.1 and a client cert, written out as PEM files."""
    ca = _make_cert("test-ca", is_ca=True)
    server = _make_cert("server", issuer=ca, sans=[x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    client = _make_cert("client", issuer=ca)
    files = {
        "ca": _cert_pem(ca[0]),
        "server_cert": _cert_pem(server[0]),
        "server_key": _key_pem(server[1]),
        "client_cert": _cert_pem(client[0]),
        "client_key": _key_pem(client[1]),
    }
    paths = {}
    for name, data in files.items():
        paths[name] = tmp_path / f"{name}.pem"
        paths[name].write_bytes(data)
    return paths, files
