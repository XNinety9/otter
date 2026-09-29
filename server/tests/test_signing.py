"""Signed firmware (#18): the server stores signatures, hands them out, and can require them."""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from otter import config, signing
from test_channels import ids
from test_ota_flow import fake_image


def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def sign(key, data: bytes) -> str:
    if isinstance(key, rsa.RSAPrivateKey):
        return base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA256())).decode()
    return base64.b64encode(key.sign(data, ec.ECDSA(hashes.SHA256()))).decode()


def public_pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def upload(client, version, signature=None):
    data = {"app": "weather", "hw": "esp32", "version": version}
    if signature is not None:
        data["signature"] = signature
    return client.post("/api/firmwares", data=data, files={"file": ("fw.bin", fake_image(version.encode()))})


@pytest.fixture
def server_key(monkeypatch):
    def use(key):
        monkeypatch.setattr(config, "SIGNING_PUBLIC_KEY", public_pem(key))
        signing.server_key.cache_clear()

    yield use
    signing.server_key.cache_clear()


def test_signature_is_stored_and_sent_to_devices(client, checkin):
    key = ec_key()
    signature = sign(key, fake_image(b"1.1.0"))
    fw = upload(client, "1.1.0", signature).json()
    assert fw["signed"] is True
    checkin()
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [ids(client)["aa:bb:cc:00:00:01"]]})
    assert checkin()["update"]["signature"] == signature


def test_unsigned_firmware_is_allowed_by_default(client):
    res = upload(client, "1.1.0")
    assert res.status_code == 201
    assert res.json()["signed"] is False


def test_garbage_signature_is_refused(client):
    res = upload(client, "1.1.0", "not base64!")
    assert (res.status_code, res.json()["detail"]) == (422, "the signature must be base64")


@pytest.mark.parametrize("make_key", [ec_key, lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)])
def test_server_requires_valid_signatures(client, server_key, make_key):
    key = make_key()
    server_key(key)
    assert upload(client, "1.1.0", sign(key, fake_image(b"1.1.0"))).status_code == 201

    res = upload(client, "1.2.0")
    assert (res.status_code, res.json()["detail"]) == (
        422, "unsigned firmware refused: this server requires signed images",
    )
    res = upload(client, "1.3.0", sign(ec_key(), fake_image(b"1.3.0")))  # someone else's key
    assert res.status_code == 422
    assert "invalid signature" in res.json()["detail"]
    res = upload(client, "1.4.0", sign(key, fake_image(b"another image")))  # signature of other data
    assert res.status_code == 422
    assert [f["version"] for f in client.get("/api/firmwares").json()] == ["1.1.0"]


def test_public_key_from_a_file(tmp_path):
    path = tmp_path / "otter-signing.pub"
    key = ec_key()
    path.write_text(public_pem(key))
    assert signing.load_public_key(str(path)).public_numbers() == key.public_key().public_numbers()
