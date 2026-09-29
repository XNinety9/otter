"""Per-device tokens (#15): the fleet key enrolls, the token authenticates, revocation blocks."""

import pytest

from otter import config
from test_channels import ids

A, B = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"
FLEET = {"X-Otter-Key": "fleet-s3cret"}


@pytest.fixture(autouse=True)
def fleet_key(monkeypatch):
    monkeypatch.setattr(config, "FLEET_KEY", "fleet-s3cret")


def body(mac, **extra):
    return {"mac": mac, "hw": "esp32", "app": "weather", "fw_version": "1.0.0", **extra}


def checkin(client, mac, headers, **extra):
    return client.post("/api/v1/checkin", json=body(mac, **extra), headers=headers)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def enroll(client, mac):
    """Checks in with the fleet key, then once with the token it got. Returns the token."""
    token = checkin(client, mac, FLEET).json()["token"]
    assert token.startswith("otd_")
    assert checkin(client, mac, bearer(token)).status_code == 200
    return token


def auth_state(client, mac):
    return next(d["auth"] for d in client.get("/api/devices").json() if d["mac"] == mac)


def test_enrollment(client):
    res = checkin(client, A, FLEET)
    assert res.status_code == 200
    assert auth_state(client, A) == "fleet_key"
    token = res.json()["token"]

    res = checkin(client, A, bearer(token))
    assert res.status_code == 200
    assert res.json()["token"] is None  # already has one
    assert auth_state(client, A) == "token"
    # From now on the fleet key isn't enough to act as this device.
    assert checkin(client, A, FLEET).status_code == 401


def test_legacy_agents_keep_working_with_the_fleet_key(client):
    first = checkin(client, A, FLEET).json()["token"]
    second = checkin(client, A, FLEET).json()["token"]
    assert first != second  # unused tokens rotate
    assert checkin(client, A, bearer(first)).status_code == 401
    assert checkin(client, A, bearer(second)).status_code == 200


def test_bad_credentials(client):
    assert checkin(client, A, {"X-Otter-Key": "wrong"}).status_code == 401
    assert checkin(client, A, bearer("otd_forged")).status_code == 401
    token = enroll(client, A)
    assert checkin(client, B, bearer(token)).status_code == 403  # another device's MAC


def test_revoking_blocks_one_device_only(client):
    token_a, token_b = enroll(client, A), enroll(client, B)
    assert client.post(f"/api/devices/{ids(client)[A]}/revoke").status_code == 200
    assert auth_state(client, A) == "revoked"

    assert checkin(client, A, bearer(token_a)).status_code == 401  # token forgotten
    assert checkin(client, A, FLEET).status_code == 403
    assert checkin(client, B, bearer(token_b)).status_code == 200  # the rest of the fleet is fine

    # Re-enrolling lets it get a new token with the fleet key.
    client.post(f"/api/devices/{ids(client)[A]}/reenroll")
    assert enroll(client, A)


def test_a_token_only_reports_for_its_own_device(client, upload):
    token_a, token_b = enroll(client, A), enroll(client, B)
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [ids(client)[A]]})
    order = checkin(client, A, bearer(token_a)).json()["update"]
    progress = f"/api/v1/deployments/{order['deployment_id']}/progress"

    assert client.post(progress, json={"state": "downloading", "progress": 5}, headers=bearer(token_b)).status_code == 403
    assert client.post(progress, json={"state": "downloading", "progress": 5}, headers=FLEET).status_code == 401
    assert client.post(progress, json={"state": "downloading", "progress": 5}, headers=bearer(token_a)).status_code == 200
    assert client.get(order["url"], headers=bearer(token_a)).status_code == 200
    assert client.get(order["url"]).status_code == 401

    client.post("/api/commands", json={"device_ids": [ids(client)[A]], "name": "identify"})
    command = checkin(client, A, bearer(token_a)).json()["commands"][0]
    result = f"/api/v1/commands/{command['id']}/result"
    assert client.post(result, json={"ok": True}, headers=bearer(token_b)).status_code == 403
    assert client.post(result, json={"ok": True}, headers=bearer(token_a)).status_code == 200


def test_approval_mode(client, monkeypatch):
    monkeypatch.setattr(config, "DEVICE_APPROVAL", True)
    res = checkin(client, A, FLEET)
    assert (res.status_code, res.json()["detail"]) == (403, "device awaiting approval in Otter")
    assert auth_state(client, A) == "awaiting_approval"  # listed, so it can be approved

    client.post(f"/api/devices/{ids(client)[A]}/approve")
    assert enroll(client, A)
