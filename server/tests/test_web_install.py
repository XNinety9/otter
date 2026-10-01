"""Installing firmware on a new board from the browser (#92)."""

from conftest import fake_image

FACTORY = b"\xff" * 0x1000 + b"\xe9" + b"\x00" * 4096  # ESP32: bootloader at 0x1000


def upload_with_factory(client, hw="esp32s3", version="1.0.0", factory=FACTORY):
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": hw, "version": version},
        files={"file": ("fw.bin", fake_image(version.encode())), "factory": ("factory.bin", factory)},
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_manifest_and_factory_image(client):
    fw = upload_with_factory(client)
    assert fw["has_factory"] is True
    manifest = client.get(f"/api/firmwares/{fw['id']}/manifest.json").json()
    assert manifest["builds"] == [{"chipFamily": "ESP32-S3", "parts": [{"path": "factory.bin", "offset": 0}]}]
    assert manifest["new_install_prompt_erase"] is True
    assert client.get(f"/api/firmwares/{fw['id']}/factory.bin").content == FACTORY


def test_chip_families(client):
    from otter.api_ui import web_install_family

    assert [web_install_family(hw) for hw in ["esp32", "esp32c6", "esp32s3-cam", "esp8266-1m", "esp32c61", "rp2040", "esp32x"]] == [
        "ESP32", "ESP32-C6", "ESP32-S3", "ESP8266", "ESP32-C61", None, None,
    ]


def test_without_factory_image(client, upload):
    fw = upload()
    assert fw["has_factory"] is False
    assert client.get(f"/api/firmwares/{fw['id']}/manifest.json").status_code == 404


def test_factory_image_added_later_and_deleted_with_the_firmware(client, upload):
    from otter.storage import factory_path

    fw = upload()
    res = client.post(f"/api/firmwares/{fw['id']}/factory", files={"factory": ("factory.bin", FACTORY)})
    assert res.json()["has_factory"] is True
    path = next(factory_path("x").parent.glob("*.factory.bin"))
    client.delete(f"/api/firmwares/{fw['id']}")
    assert not path.exists()


def test_empty_factory_image_is_refused(client):
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": "1.0.0"},
        files={"file": ("fw.bin", fake_image()), "factory": ("factory.bin", b"")},
    )
    assert res.status_code == 422
    assert client.get("/api/firmwares").json() == []
