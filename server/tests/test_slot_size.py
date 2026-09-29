"""Images too big for a device's OTA slot are never sent to it (#3)."""

from test_channels import follow, ids, pending, publish, reconcile

SMALL, BIG = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"  # test images are ~1 KB


def tag(client, mac, name="kitchen"):
    client.patch(f"/api/devices/{ids(client)[mac]}", json={"tags": [name]})


def test_slot_size_is_stored_and_kept(client, checkin):
    checkin(mac=SMALL, ota_slot_size=1000)
    checkin(mac=SMALL)  # an agent that doesn't send it doesn't erase it
    assert client.get("/api/devices").json()[0]["ota_slot_size"] == 1000


def test_deploying_a_too_big_image_is_refused(client, checkin, upload):
    checkin(mac=SMALL, ota_slot_size=1000)
    checkin(mac=BIG, ota_slot_size=1_000_000)
    fw = upload("1.1.0")
    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": list(ids(client).values())})
    assert res.status_code == 422
    assert "doesn't fit the OTA slot of aa:bb:cc:00:00:01 (1,000 bytes)" in res.json()["detail"]
    assert pending(client) == {}  # nothing reaches either device
    assert checkin(mac=SMALL, ota_slot_size=1000)["update"] is None


def test_unknown_slot_size_is_trusted(client, checkin, upload):
    checkin(mac=SMALL)
    fw = upload("1.1.0")
    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [ids(client)[SMALL]]})
    assert res.status_code == 201


def test_tags_and_rollouts_skip_devices_it_does_not_fit(client, checkin, upload):
    checkin(mac=SMALL, ota_slot_size=1000)
    checkin(mac=BIG, ota_slot_size=1_000_000)
    tag(client, SMALL), tag(client, BIG)
    fw = upload("1.1.0")
    assert client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["kitchen"]}).status_code == 201
    assert pending(client) == {BIG: "1.1.0"}

    fw2 = upload("1.2.0")
    res = client.post("/api/rollouts", json={"firmware_id": fw2["id"], "tags": ["kitchen"], "stages": [100]})
    assert res.status_code == 201, res.text
    assert pending(client) == {BIG: "1.2.0"}


def test_nobody_fits(client, checkin, upload):
    checkin(mac=SMALL, ota_slot_size=1000)
    tag(client, SMALL)
    fw = upload("1.1.0")
    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["kitchen"]})
    assert (res.status_code, res.json()["detail"]) == (422, "weather 1.1.0 is too big for every device tagged kitchen")
    res = client.post("/api/rollouts", json={"firmware_id": fw["id"], "tags": ["kitchen"]})
    assert res.status_code == 422
    assert "too big for the OTA slot" in res.json()["detail"]


def test_channels_skip_devices_it_does_not_fit(client, checkin):
    checkin(mac=SMALL, ota_slot_size=1000)
    checkin(mac=BIG, ota_slot_size=1_000_000)
    follow(client, SMALL, "stable"), follow(client, BIG, "stable")
    publish(client, "1.1.0", "stable")
    reconcile()
    assert pending(client) == {BIG: "1.1.0"}
