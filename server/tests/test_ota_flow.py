import hashlib

from conftest import fake_image


def device_id(client):
    return client.get("/api/devices").json()[0]["id"]


def test_checkin_registers_device(client, checkin):
    resp = checkin()
    assert resp["update"] is None
    [device] = client.get("/api/devices").json()
    assert device["mac"] == "aa:bb:cc:00:00:01"
    assert device["fw_version"] == "1.0.0"
    assert device["ip"] == "testclient"
    assert device["last_deployment"] is None


def test_full_update_cycle(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    dev = device_id(client)
    assert client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [dev]}).status_code == 201

    order = checkin()["update"]
    assert order["version"] == "1.1.0"
    assert order["sha256"] == hashlib.sha256(fake_image(b"1.1.0")).hexdigest()

    image = client.get(order["url"]).content
    assert hashlib.sha256(image).hexdigest() == order["sha256"]

    progress = f"/api/v1/deployments/{order['deployment_id']}/progress"
    assert client.post(progress, json={"state": "downloading", "progress": 50}).status_code == 200
    assert client.get("/api/devices").json()[0]["last_deployment"]["progress"] == 50
    assert client.post(progress, json={"state": "rebooting"}).status_code == 200

    assert checkin("1.1.0")["update"] is None
    dep = client.get("/api/devices").json()[0]["last_deployment"]
    assert (dep["status"], dep["progress"]) == ("success", 100)


def test_rollback_marks_failed(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    order = checkin()["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "rebooting"})

    assert checkin("1.0.0")["update"] is None
    dep = client.get("/api/devices").json()[0]["last_deployment"]
    assert dep["status"] == "failed"
    assert "rollback" in dep["error"]


def test_cancel_tells_device_to_abort(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    order = checkin()["update"]
    assert client.post(f"/api/deployments/{order['deployment_id']}/cancel").status_code == 200

    res = client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "downloading"})
    assert res.status_code == 409
    assert checkin()["update"] is None


def test_new_deployment_supersedes_previous(client, checkin, upload):
    checkin()
    fw1, fw2 = upload("1.1.0"), upload("1.2.0")
    dev = device_id(client)
    client.post("/api/deployments", json={"firmware_id": fw1["id"], "device_ids": [dev]})
    client.post("/api/deployments", json={"firmware_id": fw2["id"], "device_ids": [dev]})
    assert checkin()["update"]["version"] == "1.2.0"


def test_rejects_incompatible_hardware(client, checkin, upload):
    checkin(hw="esp8266")
    fw = upload("1.1.0", hw="esp32")
    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    assert res.status_code == 422


def test_rejects_non_esp_image(client):
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": "1.0.0"},
        files={"file": ("fw.bin", b"hello world")},
    )
    assert res.status_code == 422


def test_duplicate_firmware_version(client, upload):
    upload("1.1.0")
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": "1.1.0"},
        files={"file": ("fw.bin", fake_image(b"other"))},
    )
    assert res.status_code == 409


def test_fleet_key(client, checkin, monkeypatch):
    from otter import config

    monkeypatch.setattr(config, "FLEET_KEY", "s3cret")
    body = {"mac": "aa:bb:cc:00:00:02", "hw": "esp32", "app": "weather", "fw_version": "1.0.0"}
    assert client.post("/api/v1/checkin", json=body).status_code == 401
    assert client.post("/api/v1/checkin", json=body, headers={"X-Otter-Key": "s3cret"}).status_code == 200


def test_long_poll_times_out_without_update(client, checkin):
    import time

    checkin()
    body = {"mac": "aa:bb:cc:00:00:01", "hw": "esp32", "app": "weather", "fw_version": "1.0.0", "wait_s": 1}
    start = time.monotonic()
    res = client.post("/api/v1/checkin", json=body)
    assert res.json()["update"] is None
    assert 0.9 < time.monotonic() - start < 3


def test_long_poll_wakes_up_on_deployment(client, checkin, upload):
    import threading
    import time

    checkin()
    fw = upload("1.1.0")
    dev = device_id(client)
    body = {"mac": "aa:bb:cc:00:00:01", "hw": "esp32", "app": "weather", "fw_version": "1.0.0", "wait_s": 20}
    result = {}

    def poll():
        start = time.monotonic()
        result["resp"] = client.post("/api/v1/checkin", json=body).json()
        result["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.5)
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [dev]})
    thread.join(timeout=10)

    assert result["resp"]["update"]["version"] == "1.1.0"
    assert result["elapsed"] < 3


def test_device_deployment_history(client, checkin, upload):
    checkin()
    fw1, fw2 = upload("1.1.0"), upload("1.2.0")
    dev = device_id(client)
    client.post("/api/deployments", json={"firmware_id": fw1["id"], "device_ids": [dev]})
    client.post("/api/deployments", json={"firmware_id": fw2["id"], "device_ids": [dev]})
    checkin("1.2.0")

    history = client.get(f"/api/devices/{dev}/deployments").json()
    assert [(d["firmware"]["version"], d["status"]) for d in history] == [
        ("1.2.0", "success"),
        ("1.1.0", "cancelled"),
    ]
    assert history[1]["error"] == "superseded"
    assert client.get("/api/devices/999/deployments").status_code == 404
