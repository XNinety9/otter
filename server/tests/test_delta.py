"""Delta updates (#26): a patch from the image the device runs, when Otter knows it."""

import hashlib
import io
import os

import detools

from test_channels import ids

MAC = "aa:bb:cc:00:00:01"
CODE = os.urandom(200_000)  # random: compresses badly, patches well


def esp32_image(tag: bytes) -> bytes:
    """An ESP32-like image: header saying a SHA-256 is appended, then the code, then the hash."""
    header = bytes([0xE9, 3, 2, 0x20]) + bytes(19) + b"\x01"
    body = header + CODE + tag
    return body + hashlib.sha256(body).digest()


def upload(client, version, content):
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": version},
        files={"file": ("fw.bin", content)},
    )
    assert res.status_code == 201, res.text
    return res.json()


def order(client, checkin, fw, running="1.0.0"):
    checkin(mac=MAC, version=running)
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [ids(client)[MAC]]})
    return checkin(mac=MAC, version=running)["update"]


def test_a_patch_from_the_running_image(client, checkin):
    base, new = esp32_image(b"version 1.0.0"), esp32_image(b"version 1.1.0, a bit more")
    upload(client, "1.0.0", base)
    fw = upload(client, "1.1.0", new)
    update = order(client, checkin, fw)

    delta = update["delta"]
    assert delta["format"] == "detools-heatshrink"
    assert delta["base_hash"] == base[-32:].hex()  # what the device computes for its running image
    assert delta["size"] < len(new) // 20
    patch = client.get(delta["url"]).content
    assert len(patch) == delta["size"]
    rebuilt = io.BytesIO()
    detools.apply_patch(io.BytesIO(base), io.BytesIO(patch), rebuilt)
    assert rebuilt.getvalue() == new
    assert client.get(delta["url"], headers={"Range": "bytes=100-"}).content == patch[100:]


def test_no_patch_without_the_running_image(client, checkin):
    fw = upload(client, "1.1.0", esp32_image(b"1.1.0"))
    assert order(client, checkin, fw)["delta"] is None  # 1.0.0 was never uploaded


def test_no_patch_on_a_retry(client, checkin):
    upload(client, "1.0.0", esp32_image(b"1.0.0"))
    fw = upload(client, "1.1.0", esp32_image(b"1.1.0"))
    update = order(client, checkin, fw)
    assert update["delta"] is not None
    res = client.post(
        f"/api/v1/deployments/{update['deployment_id']}/progress",
        json={"state": "failed", "error": "delta patch failed", "retryable": True},
    )
    assert res.status_code == 200
    from otter.db import SessionLocal
    from otter.models import Deployment

    with SessionLocal() as session:  # the retry is due now
        session.get(Deployment, update["deployment_id"]).retry_at = None
        session.commit()
    retry = checkin(mac=MAC, version="1.0.0")["update"]
    assert retry["deployment_id"] == update["deployment_id"]
    assert retry["delta"] is None


def test_no_patch_when_it_saves_nothing(client, checkin):
    base = esp32_image(b"1.0.0")
    upload(client, "1.0.0", base)
    unrelated = bytes([0xE9]) + os.urandom(200_000)
    fw = upload(client, "1.1.0", unrelated)
    assert order(client, checkin, fw)["delta"] is None
