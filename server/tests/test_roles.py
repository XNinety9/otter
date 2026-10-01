"""Read-only accounts and the activity log (#100)."""

import io

import pytest

from otter import cli
from otter.models import User
from otter.db import SessionLocal

MAC = "aa:bb:cc:00:00:01"


def login_as(client, username, role, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("viewer-password\n"))
    cli.main(["create-user", username, "--role", role, "--password-stdin"])
    capsys.readouterr()
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": username, "password": "viewer-password"}).status_code == 200


@pytest.fixture
def viewer(client, monkeypatch, capsys):
    login_as(client, "alice", "viewer", monkeypatch, capsys)
    return client


def test_a_viewer_reads_but_cant_change(viewer, checkin, upload):
    checkin()
    device_id = viewer.get("/api/devices").json()[0]["id"]
    assert viewer.get(f"/api/devices/{device_id}/history").status_code == 200
    assert viewer.get("/api/auth/status").json()["role"] == "viewer"
    for method, path, body in [
        ("PATCH", f"/api/devices/{device_id}", {"name": "x"}),
        ("POST", "/api/commands", {"name": "reboot", "device_ids": [device_id]}),
        ("DELETE", f"/api/devices/{device_id}", None),
        ("PUT", f"/api/devices/{device_id}/config", {"values": {"a": 1}}),
    ]:
        assert viewer.request(method, path, json=body).status_code == 403, path
    # Watching live logs changes nothing: allowed.
    assert viewer.post(f"/api/devices/{device_id}/logs/watch").status_code == 200


def test_set_role(client, monkeypatch, capsys):
    login_as(client, "bob", "viewer", monkeypatch, capsys)
    cli.main(["set-role", "bob", "admin"])
    with SessionLocal() as session:
        assert session.query(User).filter_by(username="bob").one().role == "admin"
    assert client.patch("/api/devices/1", json={"name": "x"}).status_code == 404  # allowed now: unknown device


def test_changes_are_logged(client, checkin):
    checkin()
    device_id = client.get("/api/devices").json()[0]["id"]
    client.patch(f"/api/devices/{device_id}", json={"name": "Garden", "tags": ["outdoor"]})
    client.post("/api/commands", json={"name": "set_wifi", "args": {"password": "s3cret"}, "device_ids": [device_id]})
    client.put(f"/api/devices/{device_id}/config", json={"values": {"api_key": "s3cret", "interval": 5}})
    client.post(f"/api/devices/{device_id}/revoke")
    client.get("/api/devices")  # reads aren't logged
    log = client.get("/api/audit").json()
    assert [e["text"] for e in log] == [
        "revoked Garden",
        "set the configuration of Garden: api_key, interval",
        "sent set_wifi to Garden",
        f"changed {MAC}: renamed it Garden, tags #outdoor",
    ]
    assert all(e["status"] == 200 or e["status"] == 201 for e in log)
    assert "s3cret" not in str(log)
