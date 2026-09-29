from test_ota_flow import device_id


def devices_by_mac(client):
    return {d["mac"]: d for d in client.get("/api/devices").json()}


def tag(client, dev, tags):
    return client.patch(f"/api/devices/{dev}", json={"tags": tags})


def test_tags_are_normalized_and_deduplicated(client, checkin):
    checkin()
    res = tag(client, device_id(client), ["Test-Bench", " test-bench", "living room", "garden"])
    assert res.status_code == 200
    assert res.json()["tags"] == ["garden", "living-room", "test-bench"]


def test_invalid_tag_is_rejected(client, checkin):
    checkin()
    assert tag(client, device_id(client), ["no/slash"]).status_code == 422
    assert tag(client, device_id(client), ["x" * 33]).status_code == 422


def test_patching_tags_keeps_the_name_and_vice_versa(client, checkin):
    checkin()
    dev = device_id(client)
    client.patch(f"/api/devices/{dev}", json={"name": "Desk"})
    tag(client, dev, ["office"])
    assert client.get("/api/devices").json()[0]["name"] == "Desk"
    client.patch(f"/api/devices/{dev}", json={"name": "Desk 2"})
    assert client.get("/api/devices").json()[0]["tags"] == ["office"]


def test_tag_list_counts_devices_and_drops_unused_tags(client, checkin):
    checkin(mac="aa:bb:cc:00:00:01")
    checkin(mac="aa:bb:cc:00:00:02")
    ids = {d["mac"]: d["id"] for d in client.get("/api/devices").json()}
    tag(client, ids["aa:bb:cc:00:00:01"], ["garden", "prod"])
    tag(client, ids["aa:bb:cc:00:00:02"], ["prod"])
    assert client.get("/api/tags").json() == [{"name": "garden", "devices": 1}, {"name": "prod", "devices": 2}]

    tag(client, ids["aa:bb:cc:00:00:01"], ["prod"])  # garden no longer used
    client.delete(f"/api/devices/{ids['aa:bb:cc:00:00:02']}")
    assert client.get("/api/tags").json() == [{"name": "prod", "devices": 1}]


def test_deploy_to_tag_only_targets_matching_app_and_hardware(client, checkin, upload):
    checkin(mac="aa:bb:cc:00:00:01")                               # weather / esp32, tagged
    checkin(mac="aa:bb:cc:00:00:02")                               # weather / esp32, tagged
    checkin(mac="aa:bb:cc:00:00:03")                               # weather / esp32, not tagged
    checkin(mac="aa:bb:cc:00:00:04", hw="esp8266")                 # tagged, other hardware
    checkin(mac="aa:bb:cc:00:00:05", app="lights")                 # tagged, other app
    ids = {d["mac"]: d["id"] for d in client.get("/api/devices").json()}
    for mac in ("01", "02", "04", "05"):
        tag(client, ids[f"aa:bb:cc:00:00:{mac}"], ["test-bench"])
    fw = upload("1.1.0")

    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["Test-Bench"]})
    assert res.status_code == 201
    assert sorted(d["mac"] for d in res.json()) == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]

    devices = devices_by_mac(client)
    assert devices["aa:bb:cc:00:00:03"]["last_deployment"] is None
    assert devices["aa:bb:cc:00:00:04"]["last_deployment"] is None


def test_deploy_to_tag_and_devices_combined(client, checkin, upload):
    checkin(mac="aa:bb:cc:00:00:01")
    checkin(mac="aa:bb:cc:00:00:02")
    ids = {d["mac"]: d["id"] for d in client.get("/api/devices").json()}
    tag(client, ids["aa:bb:cc:00:00:01"], ["prod"])
    fw = upload("1.1.0")
    res = client.post(
        "/api/deployments",
        json={"firmware_id": fw["id"], "tags": ["prod"], "device_ids": [ids["aa:bb:cc:00:00:01"], ids["aa:bb:cc:00:00:02"]]},
    )
    assert len(res.json()) == 2  # device 01 is both listed and tagged: deployed once


def test_deploy_requires_targets_and_a_matching_tag(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    assert client.post("/api/deployments", json={"firmware_id": fw["id"]}).status_code == 422
    assert client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["nobody"]}).status_code == 422


def test_deploy_to_tag_skips_devices_already_on_the_version(client, checkin, upload):
    checkin(mac="aa:bb:cc:00:00:01", version="1.1.0")
    checkin(mac="aa:bb:cc:00:00:02")
    for d in client.get("/api/devices").json():
        tag(client, d["id"], ["prod"])
    fw = upload("1.1.0")

    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["prod"]})
    assert [d["mac"] for d in res.json()] == ["aa:bb:cc:00:00:02"]

    checkin(mac="aa:bb:cc:00:00:02", version="1.1.0")
    res = client.post("/api/deployments", json={"firmware_id": fw["id"], "tags": ["prod"]})
    assert res.status_code == 422
    assert "already runs 1.1.0" in res.json()["detail"]
