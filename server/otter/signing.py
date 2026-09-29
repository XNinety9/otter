"""Firmware signatures (#18).

Images are signed where they are built, with a private key that never reaches the server:
`openssl dgst -sha256 -sign key.pem image.bin | base64` (tools/push.sh and tools/release.sh
do it when OTTER_SIGNING_KEY is set). Devices built with the public key refuse any image
without a valid signature. With OTTER_SIGNING_PUBLIC_KEY set, the server also refuses them at
upload, so a mistake shows up before a deployment. ECDSA (P-256 recommended) and RSA keys work.
"""

import base64
import binascii
from functools import cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from . import config


def load_public_key(spec: str):
    """A PEM public key, given as its content or the path of its file."""
    pem = spec.encode() if "-----BEGIN" in spec else Path(spec).expanduser().read_bytes()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, ec.EllipticCurvePublicKey | rsa.RSAPublicKey):
        raise ValueError("the signing key must be an ECDSA or RSA public key")
    return key


@cache
def server_key():
    return load_public_key(config.SIGNING_PUBLIC_KEY) if config.SIGNING_PUBLIC_KEY else None


def decode(signature: str) -> bytes:
    try:
        raw = base64.b64decode(signature.strip(), validate=True)
    except binascii.Error:
        raise ValueError("the signature must be base64") from None
    if not 8 <= len(raw) <= 1024:
        raise ValueError("the signature has an unexpected size")
    return raw


def verify(key, image: Path, signature: str) -> None:
    """Raises ValueError unless signature (base64) is a valid signature of the image."""
    data = image.read_bytes()
    try:
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(decode(signature), data, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(decode(signature), data, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ValueError("invalid signature: not signed with the key this server expects") from None
