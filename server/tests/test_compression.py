"""Compressed images (#25): a zlib copy that agents download and inflate on the fly."""

import os
import zlib

from test_channels import ids

MAC = "aa:bb:cc:00:00:01"


def upload(client, content: bytes, version="1.1.0"):
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": version},
        files={"file": ("fw.bin", content)},
    )
    assert res.status_code == 201, res.text
    return res.json()


def order_for(client, checkin, fw):
    checkin(mac=MAC)
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [ids(client)[MAC]]})
    return checkin(mac=MAC)["update"]


def test_a_compressible_image_is_offered_compressed(client, checkin):
    image = b"\xe9" + bytes(4095) + b"code" * 5000
    fw = upload(client, image)
    assert 0 < fw["compressed_size"] < len(image) // 10
    order = order_for(client, checkin, fw)
    assert order["compressed"]["size"] == fw["compressed_size"]
    assert order["compressed"]["format"] == "zlib"
    assert order["size"] == len(image)  # what gets written and hashed

    packed = client.get(order["compressed"]["url"]).content
    assert zlib.decompress(packed) == image
    rest = client.get(order["compressed"]["url"], headers={"Range": "bytes=10-"})
    assert (rest.status_code, rest.content) == (206, packed[10:])


def test_an_incompressible_image_is_served_as_is(client, checkin):
    fw = upload(client, b"\xe9" + os.urandom(20000))
    assert fw["compressed_size"] is None
    order = order_for(client, checkin, fw)
    assert order["compressed"] is None
    assert client.get(order["url"] + "?format=zlib").status_code == 404
