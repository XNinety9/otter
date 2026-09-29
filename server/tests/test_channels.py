from otter import channels
from otter.db import SessionLocal
from test_ota_flow import fake_image


def ids(client):
    return {d["mac"]: d["id"] for d in client.get("/api/devices").json()}


def follow(client, mac, channel):
    res = client.patch(f"/api/devices/{ids(client)[mac]}", json={"channel": channel})
    assert res.status_code == 200, res.text
    return res.json()


def pending(client):
    """{mac: version it is being updated to}"""
    return {
        d["mac"]: d["last_deployment"]["firmware"]["version"]
        for d in client.get("/api/devices").json()
        if d["last_deployment"] and d["last_deployment"]["status"] == "pending"
    }


def publish(client, version, channel, app="weather"):
    res = client.post(
        "/api/firmwares",
        data={"app": app, "hw": "esp32", "version": version, "channel": channel},
        files={"file": ("fw.bin", fake_image(version.encode()))},
    )
    assert res.status_code == 201, res.text
    return res.json()


def reconcile():
    with SessionLocal() as session:
        changes = channels.reconcile(session)
        session.commit()
        return changes


A, B, C, D = (f"aa:bb:cc:00:00:0{i}" for i in range(1, 5))


def test_publishing_on_beta_updates_every_beta_device(client, checkin):
    """The acceptance criterion of #11."""
    for mac in (A, B, C, D):
        checkin(mac=mac)
    for mac in (A, B, C):
        follow(client, mac, "beta")

    publish(client, "1.6.0", "beta")
    assert pending(client) == {A: "1.6.0", B: "1.6.0", C: "1.6.0"}  # D follows no channel

    for mac in (A, B, C):
        assert checkin(mac=mac)["update"]["version"] == "1.6.0"
        checkin(mac=mac, version="1.6.0")
    assert {d["mac"]: d["fw_version"] for d in client.get("/api/devices").json()} == {
        A: "1.6.0", B: "1.6.0", C: "1.6.0", D: "1.0.0",
    }


def test_beta_follows_stable_too_and_gets_the_newest(client, checkin):
    checkin(mac=A)
    checkin(mac=B)
    follow(client, A, "beta")
    follow(client, B, "stable")

    publish(client, "1.1.0", "stable")
    assert pending(client) == {A: "1.1.0", B: "1.1.0"}
    for mac in (A, B):
        checkin(mac=mac, version="1.1.0")

    publish(client, "1.2.0-beta.1", "beta")
    assert pending(client) == {A: "1.2.0-beta.1"}
    checkin(mac=A, version="1.2.0-beta.1")

    publish(client, "1.2.0", "stable")  # the release is newer than its beta
    assert pending(client) == {A: "1.2.0", B: "1.2.0"}


def test_never_downgrades(client, checkin):
    checkin(mac=A, version="1.10.0")
    follow(client, A, "stable")
    publish(client, "1.9.0", "stable")  # "1.9.0" > "1.10.0" as strings, not as versions
    assert pending(client) == {}


def test_following_a_channel_picks_up_the_newest_firmware(client, checkin):
    checkin(mac=A)
    publish(client, "1.1.0", "stable")
    publish(client, "1.3.0", "stable")
    publish(client, "1.2.0", "stable")
    assert pending(client) == {}
    device = follow(client, A, "stable")
    assert device["channel"] == "stable"
    assert device["last_deployment"]["firmware"]["version"] == "1.3.0"


def test_leaves_open_and_failed_deployments_alone(client, checkin, upload):
    checkin(mac=A)
    manual = upload("1.0.5")
    client.post("/api/deployments", json={"firmware_id": manual["id"], "device_ids": [ids(client)[A]]})
    follow(client, A, "stable")
    publish(client, "1.1.0", "stable")
    assert pending(client) == {A: "1.0.5"}  # the manual deployment isn't replaced

    order = checkin(mac=A)["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": "sha256 mismatch"})
    reconcile()
    assert pending(client) == {A: "1.1.0"}  # the manual one is done: the channel takes over
    order = checkin(mac=A)["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": "sha256 mismatch"})
    reconcile()
    assert pending(client) == {}  # 1.1.0 failed on this device: not retried forever

    publish(client, "1.1.1", "stable")
    assert pending(client) == {A: "1.1.1"}


def test_unpublishing_and_promoting(client, checkin):
    checkin(mac=A)
    checkin(mac=B)
    follow(client, A, "beta")
    follow(client, B, "stable")
    fw = publish(client, "1.1.0", "beta")
    assert pending(client) == {A: "1.1.0"}

    res = client.patch(f"/api/firmwares/{fw['id']}", json={"channel": "stable"})  # promote
    assert res.json()["channel"] == "stable"
    assert pending(client) == {A: "1.1.0", B: "1.1.0"}


def test_staged_rollout_on_a_channel(client, checkin):
    for mac in (A, B, C):
        checkin(mac=mac)
        follow(client, mac, "beta")
    checkin(mac=D)
    fw = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": "2.0.0"},
        files={"file": ("fw.bin", fake_image(b"2.0.0"))},
    ).json()

    res = client.post("/api/rollouts", json={"firmware_id": fw["id"], "channel": "beta", "stages": [34, 100], "soak_s": 0})
    assert res.status_code == 201, res.text
    rollout = res.json()
    assert rollout["channel"] == "beta" and [s["size"] for s in rollout["stages"]] == [1, 2]
    assert client.get("/api/firmwares").json()[0]["channel"] == "beta"  # published by the rollout

    reconcile()  # followers queued in the rollout are left to it
    statuses = sorted((d["last_deployment"] or {}).get("status", "-") for d in client.get("/api/devices").json())
    assert statuses == ["-", "pending", "queued", "queued"]


def test_validation(client, checkin, upload):
    checkin(mac=A)
    assert client.patch(f"/api/devices/{ids(client)[A]}", json={"channel": "no/way"}).status_code == 422
    fw = upload("1.1.0")
    res = client.post("/api/rollouts", json={"firmware_id": fw["id"], "channel": "beta", "tags": ["x"]})
    assert res.status_code == 422
    res = client.post(
        "/api/firmwares",
        data={"app": "weather", "hw": "esp32", "version": "9.0.0", "channel": "bad channel!"},
        files={"file": ("fw.bin", fake_image(b"9"))},
    )
    assert res.status_code == 422
